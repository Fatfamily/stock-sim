import os
import json
import random
import bcrypt
import pandas as pd
import streamlit as st
import yfinance as yf
import altair as alt
from datetime import datetime, timedelta
from pymongo import MongoClient

# =========================
# 기본 설정
# =========================
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")

DEFAULT_CASH = 1_000_000
FEE_RATE = 0.0003

ADMIN_ID = "admin"
ADMIN_PW = "1q2w3e4r"

# 시뮬레이터 기본 종목 풀
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
# 역맵: "삼성전자" -> "005930.KS"
BASE_NAMES = {v: k for k, v in BASE_TICKERS.items()}


# =========================
# DB (MongoDB or local JSON)
# =========================
MONGO_URI = os.getenv("MONGO_URI")
use_mongo = False
db = None
users_collection = None

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()
        db = client["stock_simulator"]
        users_collection = db["users"]
        use_mongo = True
    except Exception as e:
        st.warning(f"⚠ MongoDB 연결 실패. 로컬 파일로 전환합니다. ({e})")
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
    if use_mongo:
        return users_collection.find_one({"username": username})
    with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
        data = f.read() or "{}"
        users = _json_loads_safe(data)
    return users.get(username)


def db_save_user(user_doc: dict):
    # 비밀번호 bytes면 문자열로 변환
    doc = dict(user_doc)
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
            raw = f.read() or "{}"
            users = _json_loads_safe(raw)
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_delete_user(username: str):
    if use_mongo:
        users_collection.delete_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            raw = f.read() or "{}"
            users = _json_loads_safe(raw)
        if username in users:
            users.pop(username)
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_get_all_users():
    if use_mongo:
        return list(users_collection.find({}))
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            raw = f.read() or "{}"
        users = _json_loads_safe(raw)
        return list(users.values())


# =========================
# 유저 생성 / 로그인
# =========================

def create_user(username: str, password: str):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."

    if db_get_user(username):
        return False, "이미 존재하는 사용자입니다."

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    # holdings / buy_prices는 종목 이름 기준으로 관리 (예: "삼성전자": 0)
    new_user = {
        "username": username,
        "password": hashed.decode("utf-8"),
        "cash": DEFAULT_CASH,
        "holdings": {name: 0 for name in BASE_TICKERS.values()},
        "buy_prices": {name: [] for name in BASE_TICKERS.values()},
        "logbook": [],  # [{time, action, stock, qty, price}]
        "trade_count": 0,
        "created_at": datetime.utcnow().isoformat()
    }

    db_save_user(new_user)
    return True, "회원가입 완료. 로그인 해주세요."


def check_login(username: str, password: str):
    # 관리자 로그인 처리
    if username == ADMIN_ID and password == ADMIN_PW:
        return True, "관리자 로그인 성공", {
            "username": ADMIN_ID,
            "is_admin": True,
        }

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


def update_user_after_trade(user: dict):
    db_save_user(user)


# =========================
# 주가 시스템
# =========================

# 우리는 서버부하 줄이려고, 하루에 한 번 실제 가격 세팅 후
# 같은 날에는 ±1000원 랜덤 흔들기만 할 거야
# 또 유저가 검색한 새로운 티커도 캐시에 넣고 같이 흔들어

if "price_state" not in st.session_state:
    st.session_state["price_state"] = {
        "last_refresh_date": None,      # "YYYY-MM-DD"
        "prices": {},                   # { "삼성전자": 71200, ... }
        "ticker_map": dict(BASE_TICKERS)  # { "005930.KS": "삼성전자", ... } 계속 확장 가능
    }


def fetch_single_ticker_price(ticker_code: str):
    """yfinance에서 단일 ticker 현재가(마지막 종가 비슷한 값) 시도."""
    try:
        data = yf.download(ticker_code, period="2d", progress=False)
        if data is None or data.empty:
            return None
        last_close = float(data["Close"].iloc[-1])
        return int(last_close)
    except Exception:
        return None


def refresh_prices_once_per_day():
    """
    오늘 처음 부를 때만 yfinance에서 BASE_TICKERS + (추가된 검색 티커들) 가격 가져옴.
    그 이후 호출에서는 ±1000 랜덤 변동만.
    """
    state = st.session_state["price_state"]
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 오늘 첫 갱신이면 yfinance로 다시 가져오기
    if state["last_refresh_date"] != today_str:
        # 모든 등록된 ticker들 (기본 + 검색으로 추가된 것들)
        tickers = list(state["ticker_map"].keys())

        new_prices = {}
        # bulk fetch 시도: 묶어서 한 번에
        try:
            data = yf.download(
                tickers,
                period="2d",
                progress=False,
                group_by="ticker"
            )
            for tkr in tickers:
                try:
                    df = data[tkr]
                    last_close = float(df["Close"].iloc[-1])
                    stock_name = state["ticker_map"][tkr]
                    new_prices[stock_name] = int(last_close)
                except Exception:
                    # fallback: 기존 값 or 랜덤
                    stock_name = state["ticker_map"][tkr]
                    prev_val = state["prices"].get(stock_name)
                    new_prices[stock_name] = prev_val if prev_val else random.randint(50_000, 300_000)
        except Exception:
            # 완전 실패하면 이전 가격/랜덤 유지
            for tkr, stock_name in state["ticker_map"].items():
                prev_val = state["prices"].get(stock_name)
                new_prices[stock_name] = prev_val if prev_val else random.randint(50_000, 300_000)

        state["prices"] = new_prices
        state["last_refresh_date"] = today_str

    else:
        # 이미 오늘 가격이 있음 -> 랜덤 흔들기
        mutated_prices = {}
        for stock_name, price in state["prices"].items():
            p2 = price + random.randint(-1000, 1000)
            if p2 < 1000:
                p2 = 1000
            mutated_prices[stock_name] = p2
        state["prices"] = mutated_prices

    return state["prices"]


def add_custom_ticker_if_valid(ticker_code: str):
    """
    유저가 검색창에 쓴 티커를 price_state에 등록.
    - ticker_code 예: "AAPL", "TSLA", "005930.KS"
    - 등록하면 ticker_map[ticker_code] = 보여줄이름 으로 추가.
      한국식 코드면 BASE_TICKERS에서처럼 한글 이름을 만들 수 없으니까,
      그냥 ticker_code 자체를 이름으로 쓴다. (ex: "AAPL")
    """
    ticker_code = ticker_code.strip()
    if not ticker_code:
        return False, "티커를 입력하세요."

    state = st.session_state["price_state"]

    # 이미 등록돼 있으면 패스
    if ticker_code in state["ticker_map"]:
        return True, "이미 등록된 티커입니다."

    # yfinance로 한 번 찍어서 실제로 존재하는지 확인
    price = fetch_single_ticker_price(ticker_code)
    if price is None:
        return False, "해당 티커의 가격을 가져올 수 없습니다. 틀린 티커일 수도 있어요."

    # 이름은 그냥 ticker_code 그대로 사용
    stock_name = ticker_code

    # 등록
    state["ticker_map"][ticker_code] = stock_name
    # 가격도 바로 반영
    state["prices"][stock_name] = price

    return True, f"{ticker_code} 등록 완료."


# =========================
# 거래
# =========================

def record_trade(user, action, stock_name, qty, price_each):
    log_entry = {
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "stock": stock_name,
        "qty": qty,
        "price": price_each
    }
    user.setdefault("logbook", []).append(log_entry)
    user["trade_count"] = user.get("trade_count", 0) + 1


def buy_stock(user: dict, stock_name: str, qty: int, current_price: int):
    cost = int(qty * current_price * (1 + FEE_RATE))
    if user["cash"] < cost:
        return False, "현금이 부족합니다."

    # 유저 holdings에 해당 종목 키가 없을 수도 있음 (검색으로 추가된 글로벌 티커 등)
    if stock_name not in user["holdings"]:
        user["holdings"][stock_name] = 0
        user["buy_prices"][stock_name] = []

    user["cash"] -= cost
    user["holdings"][stock_name] += qty
    user["buy_prices"][stock_name].append(current_price)

    record_trade(user, "BUY", stock_name, qty, current_price)
    update_user_after_trade(user)
    return True, "매수 완료"


def sell_stock(user: dict, stock_name: str, qty: int, current_price: int):
    if user["holdings"].get(stock_name, 0) < qty:
        return False, "보유 수량이 부족합니다."

    revenue = int(qty * current_price * (1 - FEE_RATE))
    user["cash"] += revenue
    user["holdings"][stock_name] -= qty

    # 평균단가 추적용 buy_prices에서 앞에서부터 제거
    bp_list = user["buy_prices"].get(stock_name, [])
    for _ in range(min(qty, len(bp_list))):
        bp_list.pop(0)

    record_trade(user, "SELL", stock_name, qty, current_price)
    update_user_after_trade(user)
    return True, "매도 완료"


# =========================
# 시각화(그래프)
# =========================

def portfolio_charts(user: dict, prices: dict):
    # 막대 그래프 (보유 평가액)
    rows = []
    for stock_name, amount in user["holdings"].items():
        if amount > 0:
            val = amount * prices.get(stock_name, 0)
            rows.append({"종목": stock_name, "평가액": val})

    if not rows:
        st.write("📊 그래프: 보유 종목이 없어서 표시할 게 없어요.")
        return

    df_val = pd.DataFrame(rows)

    st.write("📊 종목별 평가액(원)")
    bar_chart = (
        alt.Chart(df_val)
        .mark_bar()
        .encode(
            x=alt.X("종목:N", sort="-y"),
            y=alt.Y("평가액:Q")
        )
        .properties(height=250)
    )
    st.altair_chart(bar_chart, use_container_width=True)

    # 파이차트 느낌 (도넛)
    total_val = df_val["평가액"].sum()
    df_val["비율(%)"] = df_val["평가액"] / total_val * 100.0

    pie_chart = (
        alt.Chart(df_val)
        .mark_arc(innerRadius=60)  # 도넛 스타일
        .encode(
            theta="평가액:Q",
            color="종목:N",
            tooltip=["종목", "평가액", alt.Tooltip("비율(%)", format=".2f")]
        )
        .properties(height=250)
    )
    st.altair_chart(pie_chart, use_container_width=True)


# =========================
# 화면: 내 계좌 / 시장 & 매매
# =========================

def show_portfolio(user: dict, prices: dict):
    st.subheader("💼 내 계좌 상태")

    st.info(f"보유 현금: {user.get('cash', 0):,}원")

    # 테이블 준비
    table_rows = []
    for stock_name, amount in user["holdings"].items():
        if amount > 0:
            now_price = prices.get(stock_name, 0)
            table_rows.append({
                "종목": stock_name,
                "수량": amount,
                "현재가": now_price,
                "평가액": amount * now_price
            })

    if table_rows:
        df_hold = pd.DataFrame(table_rows)
        st.dataframe(df_hold, use_container_width=True)

        total_eval = sum(r["평가액"] for r in table_rows)
        st.write(f"총 평가액: {total_eval:,}원")
    else:
        st.write("보유 중인 주식이 없습니다.")

    # 최근 거래 로그
    st.subheader("📜 최근 거래")
    logs = user.get("logbook", [])
    if not logs:
        st.write("거래 내역이 없습니다.")
    else:
        for item in logs[::-1][:10]:
            st.write(
                f"{item['time']} - {item['action']} {item['stock']} x{item['qty']} @ {item['price']:,}원"
            )

    # 그래프
    portfolio_charts(user, prices)


def show_market_and_trade(user: dict):
    st.subheader("🧾 시장 & 매매")

    # 가격 갱신
    prices = refresh_prices_once_per_day()

    # --- 티커 검색 추가 구역 ---
    st.markdown("#### 🔍 티커 직접 추가하기 (예: AAPL, TSLA, 005930.KS 등)")
    search_ticker = st.text_input(
        "티커 코드 입력",
        key="search_ticker_input",
        placeholder="여기에 티커 코드 입력"
    )
    if st.button("티커 불러오기", key="ticker_fetch_btn"):
        ok, msg = add_custom_ticker_if_valid(search_ticker)
        if ok:
            st.success(msg)
        else:
            st.error(msg)
        # 티커 추가 후 다시 현재 prices에 반영
        prices = refresh_prices_once_per_day()

    # 시장 가격 표
    market_df = pd.DataFrame(
        [{"종목": name, "현재가": price} for name, price in prices.items()]
    ).sort_values("종목")
    st.dataframe(market_df, use_container_width=True)

    # 매매 UI
    st.markdown("#### 💸 매수 / 매도")
    stock_name = st.selectbox(
        "거래할 종목 선택",
        list(prices.keys()),
        key="trade_stock_select"
    )
    qty = st.number_input(
        "수량",
        min_value=1,
        step=1,
        value=1,
        key="trade_qty_input"
    )
    now_price = prices.get(stock_name, 0)

    col_buy, col_sell = st.columns(2)

    with col_buy:
        if st.button("매수", key="buy_button"):
            ok, msg = buy_stock(user, stock_name, qty, now_price)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
            # 여기서는 rerun 안 한다 (Render에서 반복 rerun 문제 피함)

    with col_sell:
        if st.button("매도", key="sell_button"):
            ok, msg = sell_stock(user, stock_name, qty, now_price)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    # 계좌 정보+그래프
    show_portfolio(user, prices)


def show_user_dashboard(user: dict):
    st.title(f"💹 {user['username']} 님의 주식 시뮬레이터")

    top_col1, top_col2 = st.columns([1, 5])
    with top_col1:
        if st.button("로그아웃", key="logout_btn"):
            st.session_state["logged_in"] = False
            st.session_state["user"] = None
            # 로그인 상태만 끊고 rerun으로 로그인 화면 복귀
            st.experimental_rerun()

    if user.get("is_admin", False):
        with top_col2:
            st.markdown("**✅ 관리자 계정으로 로그인 중**")

    # 일반 유저도 관리자도 여기서 시뮬 트레이드 가능하게 할 수 있지만
    # 관리자는 별도 화면도 있음
    show_market_and_trade(user)


# =========================
# 관리자 패널
# =========================

def show_admin_panel():
    st.title("🛠 관리자 모드")
    st.caption("계정 생성 / 삭제 / 전체 순위 관리")

    tab_create, tab_delete, tab_rank = st.tabs(["회원 생성", "회원 삭제", "순위 보기"])

    # 회원 생성
    with tab_create:
        st.subheader("새 회원 만들기")
        new_user = st.text_input("아이디", key="admin_create_user")
        new_pw = st.text_input("비밀번호", type="password", key="admin_create_pw")
        if st.button("생성", key="admin_create_btn"):
            ok, msg = create_user(new_user, new_pw)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    # 회원 삭제
    with tab_delete:
        st.subheader("회원 삭제")
        all_users = db_get_all_users()
        selectable_users = [u["username"] for u in all_users if u["username"] != ADMIN_ID]
        target = st.selectbox("삭제할 사용자", selectable_users, key="admin_delete_select")
        if st.button("삭제", key="admin_delete_btn"):
            if target:
                db_delete_user(target)
                st.success(f"{target} 삭제 완료")
                # rerun으로 새 목록 반영
                st.experimental_rerun()
            else:
                st.error("삭제할 사용자를 선택하세요.")

    # 순위 보기
    with tab_rank:
        st.subheader("💰 보유 현금 순위")
        all_users = db_get_all_users()
        # admin도 같이 표시할지? 지금은 포함함
        ranking = sorted(all_users, key=lambda u: u.get("cash", 0), reverse=True)
        df_rank = pd.DataFrame([
            {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
            for i, u in enumerate(ranking)
        ])
        st.dataframe(df_rank, use_container_width=True)


# =========================
# 공개 순위 (로그인 없이)
# =========================

def show_public_rank():
    st.title("🏆 보유 현금 순위 (전체)")
    all_users = db_get_all_users()
    ranking = sorted(all_users, key=lambda u: u.get("cash", 0), reverse=True)
    df_rank = pd.DataFrame([
        {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
        for i, u in enumerate(ranking)
    ])
    st.dataframe(df_rank, use_container_width=True)


# =========================
# 로그인 / 회원가입 / 순위보기 화면
# =========================

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

    username = st.text_input("아이디", key="auth_username")
    password = st.text_input("비밀번호", type="password", key="auth_password")

    if mode == "회원가입":
        if st.button("회원가입", key="signup_btn"):
            ok, msg = create_user(username, password)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    elif mode == "로그인":
        if st.button("로그인", key="login_btn"):
            ok, msg, user = check_login(username, password)
            if ok:
                st.success(msg)
                st.session_state["logged_in"] = True
                st.session_state["user"] = user
                # 로그인 직후 화면 전환은 rerun 써도 안전
                st.experimental_rerun()
            else:
                st.error(msg)


# =========================
# 메인
# =========================

def main():
    # 세션 기본 초기화
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False
    if "user" not in st.session_state:
        st.session_state["user"] = None

    # 로그인 상태일 때
    if st.session_state["logged_in"] and st.session_state["user"]:
        user = st.session_state["user"]

        if user.get("is_admin", False):
            # 관리자 화면 상단에 로그아웃 버튼
            top_col1, _ = st.columns([1, 5])
            with top_col1:
                if st.button("로그아웃", key="admin_logout_btn"):
                    st.session_state["logged_in"] = False
                    st.session_state["user"] = None
                    st.experimental_rerun()

            # 관리자 패널 + 동시에 시장/트레이드 화면도 보여줄지?
            # 너가 말한 건 관리자에서 계정 관리하는 게 우선이라서 여기서는 관리자 패널 먼저
            show_admin_panel()

            st.markdown("---")
            st.markdown("### 📈 (관리자용) 시뮬레이터 화면 미리보기")
            show_market_and_trade(user)
            return

        # 일반 유저일 경우
        show_user_dashboard(user)
        return

    # 비로그인 상태면 인증 화면
    show_auth_screen()


if __name__ == "__main__":
    main()
