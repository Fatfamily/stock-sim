import os
import json
import random
import bcrypt
import pandas as pd
import streamlit as st
import yfinance as yf
from datetime import datetime, timedelta

from pymongo import MongoClient

# ---------------------------
# 앱 설정
# ---------------------------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")

DEFAULT_CASH = 1_000_000
FEE_RATE = 0.0003

ADMIN_ID = "admin"
ADMIN_PW = "1q2w3e4r"

# 티커 목록 (조금 확장)
ticker_to_name = {
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
name_to_ticker = {v: k for k, v in ticker_to_name.items()}

# ---------------------------
# 저장소 (MongoDB 또는 로컬 JSON)
# ---------------------------

MONGO_URI = os.getenv("MONGO_URI")
use_mongo = False
db = None
users_collection = None

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # 연결 확인
        client.server_info()
        db = client["stock_simulator"]
        users_collection = db["users"]
        use_mongo = True
    except Exception as e:
        st.warning(f"⚠ MongoDB 연결 실패. 로컬 파일을 사용합니다. ({e})")
        use_mongo = False

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo:
    if not os.path.exists(LOCAL_USERS_FILE):
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
    """유저 1명 정보 읽기"""
    if use_mongo:
        return users_collection.find_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users_raw = f.read() or "{}"
        users = _json_loads_safe(users_raw)
        return users.get(username)


def db_save_user(user_doc: dict):
    """유저 1명 정보 저장/업데이트"""
    doc = dict(user_doc)

    # 암호를 bytes로 들고 있으면 문자열로 변환해서 저장 (JSON 호환)
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
            users_raw = f.read() or "{}"
        users = _json_loads_safe(users_raw)
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_delete_user(username: str):
    """유저 삭제"""
    if use_mongo:
        users_collection.delete_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users_raw = f.read() or "{}"
        users = _json_loads_safe(users_raw)
        if username in users:
            users.pop(username)
            with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
                f.write(_json_dumps_safe(users))


def db_get_all_users():
    """모든 유저 리스트 (관리자/순위용)"""
    if use_mongo:
        return list(users_collection.find({}))
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users_raw = f.read() or "{}"
        users = _json_loads_safe(users_raw)
        return list(users.values())


# ---------------------------
# 유저 생성 / 로그인 / 상태 초기화
# ---------------------------

def create_user(username: str, password: str):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."

    if db_get_user(username):
        return False, "이미 존재하는 사용자입니다."

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    # holdings: { "삼성전자": 0, ... }
    # buy_prices: { "삼성전자": [매수단가, ...], ... }
    user_doc = {
        "username": username,
        "password": hashed.decode("utf-8"),  # json 저장용
        "cash": DEFAULT_CASH,
        "holdings": {name: 0 for name in ticker_to_name.values()},
        "buy_prices": {name: [] for name in ticker_to_name.values()},
        "logbook": [],
        "trade_count": 0,
        "created_at": datetime.utcnow().isoformat()
    }

    db_save_user(user_doc)
    return True, "회원가입 완료. 로그인 해주세요."


def check_login(username: str, password: str):
    # 관리자 특수 처리
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
    else:
        return False, "비밀번호가 틀렸습니다.", None


def update_user_after_trade(user: dict):
    """거래 후 변경사항을 DB에 반영"""
    db_save_user(user)


# ---------------------------
# 주가 시스템 (서버 부하 최소화)
# ---------------------------

# 메모리 안에서만 유지하는 시뮬레이션 상태
if "price_state" not in st.session_state:
    # 예:
    # {
    #   "last_refresh_date": "2025-11-01",
    #   "prices": {"삼성전자": 71200, ...}
    # }
    st.session_state["price_state"] = {
        "last_refresh_date": None,
        "prices": {}
    }

def refresh_prices_once_per_day():
    """하루에 한 번만 yfinance로 실제 비슷한 종가를 가져오고, 이후에는 ±1000 랜덤으로만 흔들기.
    서버 부하 줄이려고 yfinance 호출을 되도록 최소화한다.
    """

    state = st.session_state["price_state"]
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 1) 오늘 처음 호출이면 yfinance로 새로 불러옴
    if state["last_refresh_date"] != today_str:
        tickers = list(ticker_to_name.keys())

        new_prices = {}
        try:
            data = yf.download(
                tickers,
                period="2d",  # 최근 2일치만
                progress=False,
                group_by="ticker"
            )

            for tkr in tickers:
                try:
                    df = data[tkr]
                    last_close = float(df["Close"].iloc[-1])
                    new_prices[ticker_to_name[tkr]] = int(last_close)
                except Exception:
                    # yfinance에서 못 받았으면 이전 값 유지 or fallback
                    prev_val = state["prices"].get(ticker_to_name[tkr], None)
                    if prev_val is not None:
                        new_prices[ticker_to_name[tkr]] = prev_val
                    else:
                        new_prices[ticker_to_name[tkr]] = random.randint(50_000, 300_000)
        except Exception:
            # 네트워크 막힌 경우 등 fallback
            for tkr in tickers:
                name = ticker_to_name[tkr]
                prev_val = state["prices"].get(name, None)
                if prev_val is not None:
                    new_prices[name] = prev_val
                else:
                    new_prices[name] = random.randint(50_000, 300_000)

        state["prices"] = new_prices
        state["last_refresh_date"] = today_str

    else:
        # 2) 같은 날이면 실시간처럼 ±1000원 랜덤만 살짝 가함
        mutated = {}
        for name, price in state["prices"].items():
            mutated_price = price + random.randint(-1000, 1000)
            if mutated_price < 1000:
                mutated_price = 1000
            mutated[name] = mutated_price
        state["prices"] = mutated

    # state는 st.session_state 안에 있으므로 자동 유지됨
    return state["prices"]


# ---------------------------
# 거래 기능
# ---------------------------

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

    # 매수가격 기록 중에서 앞쪽부터 소진
    bp_list = user["buy_prices"][stock_name]
    for _ in range(min(qty, len(bp_list))):
        bp_list.pop(0)

    record_trade(user, "SELL", stock_name, qty, current_price)
    update_user_after_trade(user)
    return True, "매도 완료"


# ---------------------------
# 화면 구성 요소
# ---------------------------

def show_portfolio(user: dict, prices: dict):
    st.subheader("💼 내 계좌")

    st.info(f"보유 현금: {user['cash']:,}원")

    rows = []
    for stock_name, amount in user["holdings"].items():
        if amount > 0:
            now_price = prices.get(stock_name, 0)
            rows.append({
                "종목": stock_name,
                "수량": amount,
                "현재가": now_price,
                "평가액": amount * now_price
            })

    if rows:
        df_hold = pd.DataFrame(rows)
        st.dataframe(df_hold, use_container_width=True)

        total_eval = sum(r["평가액"] for r in rows)
        st.write(f"총 평가액: {total_eval:,}원")
    else:
        st.write("보유 종목이 없습니다.")

    st.subheader("📜 최근 거래")
    logs = user.get("logbook", [])
    if not logs:
        st.write("거래 내역이 없습니다.")
    else:
        for item in logs[::-1][:10]:
            st.write(
                f"{item['time']} - {item['action']} {item['stock']} x{item['qty']} @ {item['price']:,}원"
            )


def show_market_and_trade(user: dict):
    st.subheader("🧾 시장 / 매매")

    prices = refresh_prices_once_per_day()

    # 시세표
    market_df = pd.DataFrame(
        [{"종목": name, "가격": price} for name, price in prices.items()]
    )
    st.dataframe(market_df, use_container_width=True)

    # 매매 UI
    stock_name = st.selectbox("종목 선택", list(prices.keys()), key="trade_stock_select")
    qty = st.number_input("수량", min_value=1, step=1, value=1, key="trade_qty_input")
    now_price = prices.get(stock_name, 0)

    buy_col, sell_col = st.columns(2)

    with buy_col:
        if st.button("매수", key="buy_button"):
            ok, msg = buy_stock(user, stock_name, qty, now_price)
            if ok:
                st.success(msg)
                st.experimental_rerun()
            else:
                st.error(msg)

    with sell_col:
        if st.button("매도", key="sell_button"):
            ok, msg = sell_stock(user, stock_name, qty, now_price)
            if ok:
                st.success(msg)
                st.experimental_rerun()
            else:
                st.error(msg)

    # 계좌 다시 보여주기
    show_portfolio(user, prices)


def show_user_dashboard(user: dict):
    st.title(f"💹 {user['username']} 님의 주식 시뮬레이터")
    logout_col, admin_col = st.columns([1, 5])

    with logout_col:
        if st.button("로그아웃", key="logout_btn"):
            st.session_state["logged_in"] = False
            st.session_state["user"] = None
            st.experimental_rerun()

    if user.get("is_admin", False):
        with admin_col:
            st.markdown("**관리자 계정으로 로그인됨** ✅")

    show_market_and_trade(user)


def show_admin_panel():
    st.title("🛠 관리자 모드")
    st.caption("계정 생성, 삭제, 순위 확인 가능")

    tab_create, tab_delete, tab_rank = st.tabs(["회원 생성", "회원 삭제", "순위 보기"])

    # 회원 생성
    with tab_create:
        new_user = st.text_input("새 아이디", key="admin_create_user")
        new_pw = st.text_input("새 비밀번호", type="password", key="admin_create_pw")
        if st.button("생성", key="admin_create_btn"):
            ok, msg = create_user(new_user, new_pw)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    # 회원 삭제
    with tab_delete:
        all_users = db_get_all_users()
        # admin은 삭제 목록에서 빼기
        usernames = [u["username"] for u in all_users if u["username"] != ADMIN_ID]
        target = st.selectbox("삭제할 사용자", usernames, key="admin_delete_select")
        if st.button("삭제", key="admin_delete_btn"):
            if target:
                db_delete_user(target)
                st.success(f"{target} 삭제 완료")
                st.experimental_rerun()
            else:
                st.error("삭제할 사용자를 선택하세요.")

    # 순위 보기
    with tab_rank:
        all_users = db_get_all_users()
        rank_list = sorted(
            all_users,
            key=lambda u: u.get("cash", 0),
            reverse=True
        )
        df_rank = pd.DataFrame([
            {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
            for i, u in enumerate(rank_list)
        ])
        st.dataframe(df_rank, use_container_width=True)


def show_public_rank():
    st.title("🏆 보유 현금 순위")
    all_users = db_get_all_users()
    # admin도 포함해도 됨. 빼고 싶으면 if u["username"] != ADMIN_ID 필터 가능
    rank_list = sorted(
        all_users,
        key=lambda u: u.get("cash", 0),
        reverse=True
    )
    df_rank = pd.DataFrame([
        {"순위": i + 1, "사용자": u["username"], "현금": u.get("cash", 0)}
        for i, u in enumerate(rank_list)
    ])
    st.dataframe(df_rank, use_container_width=True)


# ---------------------------
# 로그인 / 회원가입 화면
# ---------------------------

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

    # 로그인 / 회원가입 폼
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
                st.experimental_rerun()
            else:
                st.error(msg)


# ---------------------------
# 메인 엔트리
# ---------------------------

def main():
    # 세션 상태 준비
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False
    if "user" not in st.session_state:
        st.session_state["user"] = None

    # 로그인 되어 있으면 대시보드 / 관리자
    if st.session_state["logged_in"] and st.session_state["user"]:

        user = st.session_state["user"]

        # 관리자면 관리자 패널 + 로그아웃 버튼
        if user.get("is_admin", False):
            logout_col, spacer = st.columns([1, 5])
            with logout_col:
                if st.button("로그아웃", key="admin_logout_btn"):
                    st.session_state["logged_in"] = False
                    st.session_state["user"] = None
                    st.experimental_rerun()

            show_admin_panel()
            return

        # 일반 유저면 시뮬레이터 화면
        show_user_dashboard(user)
        return

    # 로그인 안 되어 있으면 인증 화면
    show_auth_screen()


if __name__ == "__main__":
    main()
