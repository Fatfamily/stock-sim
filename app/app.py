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

# ---------------------------
# 기본 설정
# ---------------------------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")

DEFAULT_CASH = 1_000_000
FEE_RATE = 0.0003

ADMIN_ID = "admin"
ADMIN_PW = "1q2w3e4r"

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
BASE_NAMES = {v: k for k, v in BASE_TICKERS.items()}

# ---------------------------
# DB 연결 (MongoDB or JSON)
# ---------------------------
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
        st.warning(f"⚠ MongoDB 연결 실패. 로컬 JSON으로 대체합니다. ({e})")

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
        users = _json_loads_safe(f.read() or "{}")
    return users.get(username)


def db_save_user(user_doc: dict):
    doc = dict(user_doc)
    if isinstance(doc.get("password"), (bytes, bytearray)):
        doc["password"] = doc["password"].decode("utf-8", errors="ignore")

    if use_mongo:
        users_collection.update_one({"username": doc["username"]}, {"$set": doc}, upsert=True)
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = _json_loads_safe(f.read() or "{}")
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(_json_dumps_safe(users))


def db_delete_user(username: str):
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
    if use_mongo:
        return list(users_collection.find({}))
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            return list(_json_loads_safe(f.read() or "{}").values())

# ---------------------------
# 유저 관리
# ---------------------------
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
        "holdings": {name: 0 for name in BASE_TICKERS.values()},
        "buy_prices": {name: [] for name in BASE_TICKERS.values()},
        "logbook": [],
        "trade_count": 0,
        "created_at": datetime.utcnow().isoformat(),
    }
    db_save_user(new_user)
    return True, "회원가입 완료. 로그인 해주세요."


def check_login(username: str, password: str):
    if username == ADMIN_ID and password == ADMIN_PW:
        return True, "관리자 로그인 성공", {"username": ADMIN_ID, "is_admin": True}
    user = db_get_user(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None
    try:
        stored_pw = user["password"].encode("utf-8")
        if bcrypt.checkpw(password.encode(), stored_pw):
            user["is_admin"] = False
            return True, "로그인 성공", user
    except Exception:
        pass
    return False, "비밀번호가 틀렸습니다.", None

# ---------------------------
# 주가 시스템
# ---------------------------
if "price_state" not in st.session_state:
    st.session_state["price_state"] = {
        "last_refresh_date": None,
        "prices": {},
        "ticker_map": dict(BASE_TICKERS),
    }

def fetch_single_ticker_price(ticker_code: str):
    try:
        data = yf.download(ticker_code, period="2d", progress=False)
        if data.empty:
            return None
        return int(data["Close"].iloc[-1])
    except Exception:
        return None

def refresh_prices_once_per_day():
    state = st.session_state["price_state"]
    today = datetime.now().strftime("%Y-%m-%d")

    if state["last_refresh_date"] != today:
        tickers = list(state["ticker_map"].keys())
        new_prices = {}
        try:
            data = yf.download(tickers, period="2d", progress=False, group_by="ticker")
            for tkr in tickers:
                df = data[tkr]
                new_prices[state["ticker_map"][tkr]] = int(df["Close"].iloc[-1])
        except Exception:
            for tkr, name in state["ticker_map"].items():
                new_prices[name] = random.randint(50000, 300000)
        state["prices"] = new_prices
        state["last_refresh_date"] = today
    else:
        for name in state["prices"]:
            state["prices"][name] = max(1000, state["prices"][name] + random.randint(-1000, 1000))
    return state["prices"]

def add_custom_ticker_if_valid(ticker_code: str):
    state = st.session_state["price_state"]
    ticker_code = ticker_code.strip().upper()
    if ticker_code in state["ticker_map"]:
        return True, "이미 등록된 티커입니다."
    price = fetch_single_ticker_price(ticker_code)
    if not price:
        return False, "티커를 불러올 수 없습니다."
    state["ticker_map"][ticker_code] = ticker_code
    state["prices"][ticker_code] = price
    return True, f"{ticker_code} 등록 완료."

# ---------------------------
# 거래 기능
# ---------------------------
def record_trade(user, action, stock, qty, price):
    user["logbook"].append({
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "stock": stock,
        "qty": qty,
        "price": price
    })

def buy_stock(user, stock, qty, price):
    cost = int(qty * price * (1 + FEE_RATE))
    if user["cash"] < cost:
        return False, "현금 부족"
    user["cash"] -= cost
    user["holdings"].setdefault(stock, 0)
    user["buy_prices"].setdefault(stock, [])
    user["holdings"][stock] += qty
    user["buy_prices"][stock].append(price)
    record_trade(user, "BUY", stock, qty, price)
    db_save_user(user)
    return True, "매수 완료"

def sell_stock(user, stock, qty, price):
    if user["holdings"].get(stock, 0) < qty:
        return False, "보유 수량 부족"
    user["cash"] += int(qty * price * (1 - FEE_RATE))
    user["holdings"][stock] -= qty
    record_trade(user, "SELL", stock, qty, price)
    db_save_user(user)
    return True, "매도 완료"

# ---------------------------
# 그래프
# ---------------------------
def portfolio_charts(user, prices):
    df = pd.DataFrame([
        {"종목": k, "평가액": v * prices.get(k, 0)}
        for k, v in user["holdings"].items() if v > 0
    ])
    if df.empty:
        st.write("보유 종목이 없습니다.")
        return
    bar = alt.Chart(df).mark_bar().encode(x="종목", y="평가액").properties(height=200)
    pie = alt.Chart(df).mark_arc(innerRadius=60).encode(
        theta="평가액", color="종목", tooltip=["종목", "평가액"]
    ).properties(height=200)
    st.altair_chart(bar, use_container_width=True)
    st.altair_chart(pie, use_container_width=True)

# ---------------------------
# UI 구성
# ---------------------------
def show_market_and_trade(user):
    st.subheader("🧾 시장 & 거래")
    prices = refresh_prices_once_per_day()

    ticker_input = st.text_input("🔍 티커 추가 (예: AAPL, TSLA, 005930.KS)", key="ticker_input")
    if st.button("불러오기", key="load_ticker_btn"):
        ok, msg = add_custom_ticker_if_valid(ticker_input)
        st.success(msg) if ok else st.error(msg)
        prices = refresh_prices_once_per_day()

    st.dataframe(pd.DataFrame(prices.items(), columns=["종목", "현재가"]), use_container_width=True)
    stock = st.selectbox("종목 선택", list(prices.keys()), key="trade_select")
    qty = st.number_input("수량", 1, 10000, 1, key="trade_qty")
    price = prices[stock]
    c1, c2 = st.columns(2)
    with c1:
        if st.button("매수", key="buy_btn"):
            ok, msg = buy_stock(user, stock, qty, price)
            st.success(msg) if ok else st.error(msg)
    with c2:
        if st.button("매도", key="sell_btn"):
            ok, msg = sell_stock(user, stock, qty, price)
            st.success(msg) if ok else st.error(msg)
    show_portfolio(user, prices)

def show_portfolio(user, prices):
    st.subheader("💼 내 계좌")
    st.info(f"현금: {user['cash']:,}원")
    rows = []
    for name, qty in user["holdings"].items():
        if qty > 0:
            now = prices.get(name, 0)
            rows.append({"종목": name, "수량": qty, "현재가": now, "평가액": qty * now})
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
    portfolio_charts(user, prices)

def show_user_dashboard(user):
    st.title(f"📊 {user['username']} 님의 시뮬레이터")
    if st.button("로그아웃", key="logout_btn"):
        st.session_state["logged_in"] = False
        st.session_state["user"] = None
        st.rerun()
    show_market_and_trade(user)

def show_admin_panel():
    st.title("🛠 관리자 모드")
    t1, t2, t3 = st.tabs(["회원 생성", "회원 삭제", "순위 보기"])
    with t1:
        u = st.text_input("아이디", key="admin_user")
        p = st.text_input("비밀번호", type="password", key="admin_pw")
        if st.button("생성", key="admin_create"):
            ok, msg = create_user(u, p)
            st.success(msg) if ok else st.error(msg)
    with t2:
        users = [u["username"] for u in db_get_all_users() if u["username"] != ADMIN_ID]
        target = st.selectbox("삭제할 회원", users, key="admin_del_select")
        if st.button("삭제", key="admin_delete"):
            db_delete_user(target)
            st.success(f"{target} 삭제 완료")
            st.rerun()
    with t3:
        df = pd.DataFrame(
            sorted(db_get_all_users(), key=lambda x: x.get("cash", 0), reverse=True)
        )
        st.dataframe(df[["username", "cash"]], use_container_width=True)

def show_auth_screen():
    st.title("📈 가상 주식 시뮬레이터")
    mode = st.radio("메뉴 선택", ["로그인", "회원가입", "순위보기"], key="mode_radio")
    if mode == "순위보기":
        df = pd.DataFrame(
            sorted(db_get_all_users(), key=lambda x: x.get("cash", 0), reverse=True)
        )
        st.dataframe(df[["username", "cash"]], use_container_width=True)
        return
    username = st.text_input("아이디", key="auth_user")
    password = st.text_input("비밀번호", type="password", key="auth_pw")
    if mode == "회원가입":
        if st.button("회원가입", key="signup_btn"):
            ok, msg = create_user(username, password)
            st.success(msg) if ok else st.error(msg)
    else:
        if st.button("로그인", key="login_btn"):
            ok, msg, user = check_login(username, password)
            if ok:
                st.success(msg)
                st.session_state["logged_in"] = True
                st.session_state["user"] = user
                st.rerun()
            else:
                st.error(msg)

def main():
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False
        st.session_state["user"] = None
    if st.session_state["logged_in"] and st.session_state["user"]:
        u = st.session_state["user"]
        if u.get("is_admin", False):
            if st.button("로그아웃", key="admin_logout"):
                st.session_state["logged_in"] = False
                st.session_state["user"] = None
                st.rerun()
            show_admin_panel()
            st.markdown("---")
            st.markdown("### 📈 시뮬레이터 (관리자용)")
            show_market_and_trade(u)
        else:
            show_user_dashboard(u)
    else:
        show_auth_screen()

if __name__ == "__main__":
    main()
