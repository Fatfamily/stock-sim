import os
import json
import random
import bcrypt
import pandas as pd
import streamlit as st
import yfinance as yf
import altair as alt
from datetime import datetime
from pymongo import MongoClient

# ---------------------------------
# 기본 앱 설정
# ---------------------------------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")

DEFAULT_CASH = 1_000_000
FEE_RATE = 0.0003  # 수수료 비율

ADMIN_ID = "admin"
ADMIN_PW = "1q2w3e4r"

# 기본으로 보여줄 한국 대형주들
BASE_TICKERS = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "373220.KQ": "LG에너지솔루션",
    "005380.KS": "현대차",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
    "051910.KS": "LG화학",
    "068270.KS": "셀트리온",
    "105560.KS": "KB금융",
    "028260.KS": "삼성물산",
}
# 역매핑: "삼성전자" -> "005930.KS"
BASE_NAMES = {v: k for k, v in BASE_TICKERS.items()}


# ---------------------------------
# 저장소 (MongoDB 있으면 사용, 없으면 로컬 JSON)
# ---------------------------------
MONGO_URI = os.getenv("MONGO_URI")
use_mongo = False
db = None
users_collection = None

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()  # 연결 확인
        db = client["stock_simulator"]
        users_collection = db["users"]
        use_mongo = True
    except Exception as e:
        st.warning(f"⚠ MongoDB 연결 실패. 로컬 JSON으로 전환합니다. ({e})")
        use_mongo = False

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo and not os.path.exists(LOCAL_USERS_FILE):
    with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
        f.write("{}")


def _json_loads_safe(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _json_dumps_safe(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return "{}"


def db_get_user(username: str):
    """유저 한 명의 정보를 dict로 리턴. 없으면 None."""
    if use_mongo:
        return users_collection.find_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = _json_loads_safe(f.read() or "{}")
        return users.get(username)


def db_save_user(user_doc: dict):
    """유저 정보 저장/업데이트"""
    doc = dict(user_doc)

    # 비밀번호가 bytes일 경우 JSON 저장 가능하게 문자열화
    if isinstance(doc.get("password"), (bytes, bytearray)):
        doc["password"] = doc["password"].decode("utf-8", errors="ignore")

    if use_mongo:
        users_collection.update_one(
            {"username": doc["username"]},
            {"$set": doc},
            upsert=True
        )
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = _json_loads_safe(f.read() or "{}")
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_delete_user(username: str):
    """유저 삭제 (관리자 기능)"""
    if use_mongo:
        users_collection.delete_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = _json_loads_safe(f.read() or "{}")
        if username in users:
            users.pop(username)
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_get_all_users():
    """전체 유저 목록 리스트로 반환"""
    if use_mongo:
        return list(users_collection.find({}))
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            return list(_json_loads_safe(f.read() or "{}").values())


# ---------------------------------
# 유저 생성 / 로그인
# ---------------------------------
def create_user(username: str, password: str):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."

    if db_get_user(username):
        return False, "이미 존재하는 사용자입니다."

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    new_user = {
        "username": username,
        "password": hashed.decode("utf-8"),
        "cash": DEFAULT_CASH,
        "holdings": {name: 0 for name in BASE_TICKERS.values()},  # {"삼성전자":0, ...}
        "buy_prices": {name: [] for name in BASE_TICKERS.values()},
        "logbook": [],  # 최근 거래 기록
        "trade_count": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    db_save_user(new_user)
    return True, "회원가입 완료. 로그인 해주세요."


def check_login(username: str, password: str):
    # 관리자
    if username == ADMIN_ID and password == ADMIN_PW:
        return True, "관리자 로그인 성공", {"username": ADMIN_ID, "is_admin": True}

    user = db_get_user(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None

    stored_pw = user.get("password", "")
    try:
        stored_pw_bytes = stored_pw.encode("utf-8")
    except Exception:
        stored_pw_bytes = stored_pw

    if bcrypt.checkpw(password.encode(), stored_pw_bytes):
        user["is_admin"] = False
        return True, "로그인 성공", user

    return False, "비밀번호가 틀렸습니다.", None


def save_user_after_trade(user: dict):
    db_save_user(user)


# ---------------------------------
# 시세 관리 (yfinance 캐싱)
# ---------------------------------
# Streamlit 세션 상태에 오늘자 시세와 등록된 티커 목록을 유지
if "price_state" not in st.session_state:
    st.session_state["price_state"] = {
        "last_refresh_date": None,          # "YYYY-MM-DD"
        "prices": {},                       # {"삼성전자": 71000, "AAPL": 210000, ...}
        "ticker_map": dict(BASE_TICKERS),   # {"005930.KS":"삼성전자", ... , "AAPL":"AAPL"}
    }


def fetch_single_ticker_price(ticker_code: str):
    """
    단일 티커 코드(yfinance용)에서 마지막 종가 비슷한 값을 int로 반환.
    실패하면 None.
    """
    try:
        data = yf.download(ticker_code, period="2d", progress=False)
        if data is None or data.empty:
            return None
        last_close = float(data["Close"].iloc[-1])
        return int(last_close)
    except Exception:
        return None


def guess_ticker_from_name_or_code(query: str):
    """
    사용자가 입력한 문자열(query)이:
    - 이미 정확한 티커일 수도 있고 (예: "AAPL", "005930.KS")
    - 한국어/영어 회사명일 수도 있음 (예: "삼성전자", "레인보우로보틱스", "Rainbow Robotics")
    이걸 yfinance Search로 가능한 티커 하나 찾아서 돌려준다.

    성공하면 (ticker_code, display_name)
    실패하면 (None, error_msg)
    """
    q = query.strip()
    if not q:
        return None, "티커 또는 회사명을 입력하세요."

    # 1) 먼저 그대로 티커로 시도
    price_direct = fetch_single_ticker_price(q)
    if price_direct is not None:
        # 그대로 사용가능
        return q, q  # (티커코드, 화면에 보여줄 이름)

    # 2) yfinance 검색 API 사용 (회사명 -> 티커)
    # yfinance.Search는 0.2.66 버전에 존재하고 .quotes에 결과 리스트가 들어있음
    # quotes[i] 예: {'symbol': 'AAPL', 'shortname': 'Apple Inc.', 'longname': 'Apple Inc.' ...}
    try:
        search_obj = yf.Search(q, max_results=5)
        quotes = getattr(search_obj, "quotes", [])
    except Exception:
        quotes = []

    if not quotes:
        return None, f"'{q}' 에 해당하는 종목을 찾을 수 없습니다."

    # 첫 번째 후보를 사용
    cand = quotes[0]
    ticker_code = cand.get("symbol")
    display_name = cand.get("shortname") or cand.get("longname") or ticker_code

    if not ticker_code:
        return None, f"'{q}' 에서 유효한 티커를 찾지 못했습니다."

    # 마지막으로 한 번 더 실제 가격 확인 (존재 확인)
    test_price = fetch_single_ticker_price(ticker_code)
    if test_price is None:
        return None, f"{ticker_code} 가격 정보를 가져올 수 없습니다."

    return ticker_code, display_name


def refresh_prices_once_per_day():
    """
    하루에 한 번은 yfinance로 모든 등록 티커의 '마지막 종가'를 가져와서 갱신.
    같은 날 안에서는 이미 있는 가격들을 ±1000원 랜덤으로 흔들어서 '실시간 느낌'.
    """

    state = st.session_state["price_state"]
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 오늘 첫 호출이면 실제 데이터 가져옴
    if state["last_refresh_date"] != today_str:
        tickers = list(state["ticker_map"].keys())  # ["005930.KS", "AAPL", ...]
        new_prices = {}

        try:
            # 한 번에 다운(성능/요금 줄이기)
            data = yf.download(tickers, period="2d", progress=False, group_by="ticker")

            # data는 ticker별로 df 들어있다고 가정
            for tkr in tickers:
                try:
                    df = data[tkr]
                    last_close = float(df["Close"].iloc[-1])
                    display_name = state["ticker_map"][tkr]
                    new_prices[display_name] = int(last_close)
                except Exception:
                    # 이 티커만 개별 fallback
                    display_name = state["ticker_map"][tkr]
                    prev_val = state["prices"].get(display_name)
                    if prev_val is not None:
                        new_prices[display_name] = prev_val
                    else:
                        new_prices[display_name] = random.randint(50_000, 300_000)

        except Exception:
            # 전체 실패 시 전부 fallback
            for tkr, display_name in state["ticker_map"].items():
                prev_val = state["prices"].get(display_name)
                if prev_val is not None:
                    new_prices[display_name] = prev_val
                else:
                    new_prices[display_name] = random.randint(50_000, 300_000)

        state["prices"] = new_prices
        state["last_refresh_date"] = today_str

    else:
        # 이미 오늘 가격을 가져온 상태라면 ±1000원 랜덤 변동
        mutated = {}
        for display_name, old_price in state["prices"].items():
            p2 = old_price + random.randint(-1000, 1000)
            if p2 < 1000:
                p2 = 1000
            mutated[display_name] = p2
        state["prices"] = mutated

    return state["prices"]


def register_new_ticker_from_user_input(user_query: str):
    """
    사용자가 검색창에 입력한 문자열을 기반으로:
    1) 회사명/티커를 yfinance Search로 찾음
    2) state에 ticker_map 과 prices를 등록
    """
    user_query = user_query.strip()
    if not user_query:
        return False, "값을 입력하세요."

    state = st.session_state["price_state"]

    # 이미 등록된 티커인지 확인
    # state["ticker_map"]는 { "005930.KS":"삼성전자", "AAPL":"Apple Inc." ... }
    # 이미 등록된 display_name도 확인
    if user_query in state["ticker_map"].keys() or user_query in state["ticker_map"].values():
        return True, "이미 등록된 종목입니다."

    ticker_code, display_name_or_err = guess_ticker_from_name_or_code(user_query)
    if ticker_code is None:
        return False, display_name_or_err  # display_name_or_err는 에러 메시지

    display_name = display_name_or_err

    # 중복 display_name 방지: 만약 display_name이 이미 price dict key로 쓰이고 있으면 이름에 티커 붙임
    if display_name in state["prices"]:
        display_name = f"{display_name} ({ticker_code})"

    # ticker_map에 추가
    state["ticker_map"][ticker_code] = display_name

    # 현재 가격도 즉시 한 번 가져와 반영
    current_price = fetch_single_ticker_price(ticker_code)
    if current_price is None:
        current_price = random.randint(50_000, 300_000)

    state["prices"][display_name] = current_price

    return True, f"{display_name} ({ticker_code}) 추가 완료."


# ---------------------------------
# 거래 관련 로직
# ---------------------------------
def record_trade(user, action, stock_name, qty, price_each):
    user["logbook"].append({
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "stock": stock_name,
        "qty": qty,
        "price": price_each
    })
    user["trade_count"] = user.get("trade_count", 0) + 1


def buy_stock(user: dict, stock_name: str, qty: int, now_price: int):
    total_cost = int(qty * now_price * (1 + FEE_RATE))
    if user["cash"] < total_cost:
        return False, "현금이 부족합니다."

    # 새로운 종목도 살 수 있게 세팅
    user["holdings"].setdefault(stock_name, 0)
    user["buy_prices"].setdefault(stock_name, [])

    user["cash"] -= total_cost
    user["holdings"][stock_name] += qty
    user["buy_prices"][stock_name].append(now_price)

    record_trade(user, "BUY", stock_name, qty, now_price)
    save_user_after_trade(user)
    return True, "매수 완료"


def sell_stock(user: dict, stock_name: str, qty: int, now_price: int):
    if user["holdings"].get(stock_name, 0) < qty:
        return False, "보유 수량이 부족합니다."

    total_rev = int(qty * now_price * (1 - FEE_RATE))

    user["cash"] += total_rev
    user["holdings"][stock_name] -= qty

    # 평균단가 관리 (앞에서부터 소진)
    bp_list = user["buy_prices"].get(stock_name, [])
    for _ in range(min(qty, len(bp_list))):
        bp_list.pop(0)

    record_trade(user, "SELL", stock_name, qty, now_price)
    save_user_after_trade(user)
    return True, "매도 완료"


# ---------------------------------
# 포트폴리오 시각화
# ---------------------------------
def portfolio_charts(user: dict, prices: dict):
    rows = []
    for stock_name, amount in user["holdings"].items():
        if amount > 0:
            now_price = prices.get(stock_name, 0)
            rows.append({
                "종목": stock_name,
                "평가액": amount * now_price
            })

    if not rows:
        st.write("보유 종목이 없습니다.")
        return

    df_val = pd.DataFrame(rows)
    total_val = df_val["평가액"].sum()

    # 막대 그래프
    st.write("📊 종목별 평가액")
    bar_chart = (
        alt.Chart(df_val)
        .mark_bar()
        .encode(
            x=alt.X("종목:N", sort="-y"),
            y=alt.Y("평가액:Q")
        )
        .properties(height=220)
    )
    st.altair_chart(bar_chart, use_container_width=True)

    # 도넛형 파이차트 (비율)
    st.write("🍩 포트폴리오 비율")
    df_val["비율(%)"] = df_val["평가액"] / total_val * 100.0
    pie_chart = (
        alt.Chart(df_val)
        .mark_arc(innerRadius=60)
        .encode(
            theta="평가액:Q",
            color="종목:N",
            tooltip=["종목", "평가액", alt.Tooltip("비율(%)", format=".2f")]
        )
        .properties(height=220)
    )
    st.altair_chart(pie_chart, use_container_width=True)


# ---------------------------------
# 뷰: 내 포트폴리오
# ---------------------------------
def show_portfolio(user: dict, prices: dict):
    st.subheader("💼 내 계좌")

    st.info(f"보유 현금: {user.get('cash', 0):,}원")

    table_rows = []
    for stock_name, qty in user["holdings"].items():
        if qty > 0:
            now_price = prices.get(stock_name, 0)
            table_rows.append({
                "종목": stock_name,
                "수량": qty,
                "현재가": now_price,
                "평가액": qty * now_price
            })

    if table_rows:
        df_hold = pd.DataFrame(table_rows)
        st.dataframe(df_hold, use_container_width=True)
        total_eval = sum(r["평가액"] for r in table_rows)
        st.write(f"총 평가액: {total_eval:,}원")
    else:
        st.write("보유 중인 주식이 없습니다.")

    st.subheader("📜 최근 거래")
    logs = user.get("logbook", [])
    if logs:
        for e in logs[::-1][:10]:
            st.write(
                f"{e['time']} - {e['action']} {e['stock']} x{e['qty']} @ {e['price']:,}원"
            )
    else:
        st.write("거래 내역 없음")

    portfolio_charts(user, prices)


# ---------------------------------
# 뷰: 시장 / 거래
# ---------------------------------
def show_market_and_trade(user: dict):
    st.subheader("🧾 시장 & 거래")

    prices = refresh_prices_once_per_day()

    # ---- 티커 추가 / 종목 이름 검색 ----
    st.markdown("#### 🔍 종목 추가")
    st.caption("티커 코드(AAPL, TSLA, 005930.KS) 또는 회사명(삼성전자, 레인보우로보틱스 등)을 입력하세요.")
    query = st.text_input("검색", key="ticker_search_input", placeholder="예: 레인보우로보틱스 / AAPL / 삼성전자")
    if st.button("불러오기", key="ticker_search_button"):
        ok, msg = register_new_ticker_from_user_input(query)
        if ok:
            st.toast(msg, icon="✅")
        else:
            st.toast(msg, icon="⚠")
        prices = refresh_prices_once_per_day()

    # 현재 시세표
    st.markdown("#### 📈 현재 시세")
    market_df = pd.DataFrame(
        [{"종목": n, "현재가": p} for n, p in prices.items()]
    ).sort_values("종목")
    st.dataframe(market_df, use_container_width=True)

    st.markdown("#### 💸 매수 / 매도")
    stock_name = st.selectbox(
        "종목 선택",
        list(prices.keys()),
        key="trade_stock_selectbox"
    )
    qty = st.number_input(
        "수량",
        min_value=1,
        step=1,
        value=1,
        key="trade_qty_input"
    )
    now_price = prices.get(stock_name, 0)

    buy_col, sell_col = st.columns(2)
    with buy_col:
        if st.button("매수", key="buy_button"):
            ok, msg = buy_stock(user, stock_name, qty, now_price)
            if ok:
                st.toast(msg, icon="🟢")
            else:
                st.toast(msg, icon="⚠")

    with sell_col:
        if st.button("매도", key="sell_button"):
            ok, msg = sell_stock(user, stock_name, qty, now_price)
            if ok:
                st.toast(msg, icon="🔴")
            else:
                st.toast(msg, icon="⚠")

    # 내 계좌 / 그래프
    show_portfolio(user, prices)


# ---------------------------------
# 일반 유저 대시보드
# ---------------------------------
def show_user_dashboard(user: dict):
    st.title(f"📊 {user['username']} 님의 시뮬레이터")

    top_col1, top_col2 = st.columns([1, 5])
    with top_col1:
        if st.button("로그아웃", key="logout_btn"):
            st.session_state["logged_in"] = False
            st.session_state["user"] = None
            st.rerun()

    if user.get("is_admin", False):
        with top_col2:
            st.markdown("**✅ 관리자 계정으로 로그인 중**")

    show_market_and_trade(user)


# ---------------------------------
# 관리자 화면
# ---------------------------------
def show_admin_panel():
    st.title("🛠 관리자 모드")

    # 상단 로그아웃은 main()에서 처리

    tab_create, tab_delete, tab_rank = st.tabs(["회원 생성", "회원 삭제", "순위 보기"])

    # 회원 생성
    with tab_create:
        st.subheader("새 회원 만들기")
        new_user = st.text_input("아이디", key="admin_new_user_input")
        new_pw = st.text_input("비밀번호", type="password", key="admin_new_pw_input")
        if st.button("생성하기", key="admin_create_user_button"):
            ok, msg = create_user(new_user, new_pw)
            if ok:
                st.toast(msg, icon="✅")
            else:
                st.toast(msg, icon="⚠")

    # 회원 삭제
    with tab_delete:
        st.subheader("회원 삭제")
        all_users = db_get_all_users()
        selectable_users = [
            u["username"] for u in all_users
            if u["username"] != ADMIN_ID
        ]
        del_user = st.selectbox("삭제할 사용자", selectable_users, key="admin_delete_select")
        if st.button("삭제하기", key="admin_delete_button"):
            if del_user:
                db_delete_user(del_user)
                st.toast(f"{del_user} 삭제 완료", icon="🗑")
                st.rerun()
            else:
                st.toast("삭제할 사용자를 선택하세요.", icon="⚠")

    # 순위 보기
    with tab_rank:
        st.subheader("💰 보유 현금 순위")
        all_users = db_get_all_users()
        ranking = sorted(all_users, key=lambda u: u.get("cash", 0), reverse=True)

        df_rank = pd.DataFrame([
            {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
            for i, u in enumerate(ranking)
        ])
        st.dataframe(df_rank, use_container_width=True)


# ---------------------------------
# 공개 순위 (로그인 없이)
# ---------------------------------
def show_public_rank():
    st.title("🏆 전체 사용자 현금 순위")
    all_users = db_get_all_users()
    ranking = sorted(all_users, key=lambda u: u.get("cash", 0), reverse=True)

    df_rank = pd.DataFrame([
        {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
        for i, u in enumerate(ranking)
    ])
    st.dataframe(df_rank, use_container_width=True)


# ---------------------------------
# 로그인 / 회원가입 / 순위 화면
# ---------------------------------
def show_auth_screen():
    st.title("📈 가상 주식 시뮬레이터")

    mode = st.radio(
        "메뉴 선택",
        ["로그인", "회원가입", "순위보기"],
        key="auth_mode_radio"
    )

    if mode == "순위보기":
        show_public_rank()
        return

    username = st.text_input("아이디", key="auth_username_input")
    password = st.text_input("비밀번호", type="password", key="auth_password_input")

    if mode == "회원가입":
        if st.button("회원가입", key="signup_button"):
            ok, msg = create_user(username, password)
            if ok:
                st.toast(msg, icon="✅")
            else:
                st.toast(msg, icon="⚠")

    elif mode == "로그인":
        if st.button("로그인", key="login_button"):
            ok, msg, user = check_login(username, password)
            if ok:
                st.toast(msg, icon="✅")
                st.session_state["logged_in"] = True
                st.session_state["user"] = user
                st.rerun()
            else:
                st.toast(msg, icon="⚠")


# ---------------------------------
# 메인 (라우팅)
# ---------------------------------
def main():
    # 세션 초기화
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False
        st.session_state["user"] = None

    # 로그인된 상태인지 확인
    if st.session_state["logged_in"] and st.session_state["user"]:
        current_user = st.session_state["user"]

        # 관리자
        if current_user.get("is_admin", False):
            top_col1, _ = st.columns([1, 5])
            with top_col1:
                if st.button("로그아웃", key="admin_logout_btn"):
                    st.session_state["logged_in"] = False
                    st.session_state["user"] = None
                    st.rerun()

            show_admin_panel()

            st.markdown("---")
            st.markdown("### 📈 시뮬레이터 (관리자 미리보기)")
            show_market_and_trade(current_user)
            return

        # 일반 유저
        show_user_dashboard(current_user)
        return

    # 로그인 안 되어 있으면 여기
    show_auth_screen()


if __name__ == "__main__":
    main()
