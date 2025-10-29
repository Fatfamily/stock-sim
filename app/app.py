import os
import streamlit as st
from pymongo import MongoClient, errors
import bcrypt
import random
from datetime import datetime, timedelta
import yfinance as yf
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import altair as alt

# ---------- 설정 ----------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")
DEFAULT_CASH = 1000000
DEFAULT_CREDIT = 100
FEE_RATE = 0.0003  # 거래 수수료 비율 (예시)
TRADE_LIMIT_BEFORE_PENALTY = 5

# ---------- 티커와 한글명 매핑 ----------
ticker_to_name = {
"005930.KS": "삼성전자",
"000660.KS": "SK하이닉스",
"373220.KQ": "LG에너지솔루션",
"005380.KS": "현대차",
"035420.KS": "NAVER",
"035720.KS": "카카오",
"012330.KS": "현대모비스",
"051910.KS": "LG화학",
"068270.KS": "셀트리온",
"207940.KQ": "삼성바이오로직스",
"055550.KS": "신한지주",
"105560.KS": "KB금융",
"005490.KS": "POSCO홀딩스",
"096770.KS": "SK이노베이션",
"003550.KS": "LG",
"015760.KS": "한국전력",
"086790.KQ": "하나금융지주",
"034020.KS": "두산에너빌리티",
"066570.KS": "LG전자",
"028260.KS": "삼성물산",
}
name_to_ticker = {v: k for k, v in ticker_to_name.items()}

# ---------- 데이터 저장 방식 (MongoDB or local fallback) ----------
MONGO_URI = os.getenv("MONGO_URI")
db = None
users_collection = None
use_mongo = False
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()
        db = client["stock_simulator"]
        users_collection = db["users"]
        use_mongo = True
    except Exception as e:
        st.warning(f"MongoDB 연결 실패, 로컬 파일로 대체합니다. ({e})")
        use_mongo = False

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo:
    if not os.path.exists(LOCAL_USERS_FILE):
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write("{}")

def json_safe_loads(s):
    try:
        import json
        return json.loads(s)
    except Exception:
        return {}

def json_safe_dumps(obj):
    try:
        import json
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return "{}"

def get_user_record(username):
    if use_mongo:
        return users_collection.find_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            data = f.read() or "{}"
            users = json_safe_loads(data)
        return users.get(username)

def save_user_record(user_doc):
    # Do not store raw bytes in local JSON; convert to str
    doc = dict(user_doc)
    if isinstance(doc.get("password"), (bytes, bytearray)):
        doc["password"] = doc["password"].decode('utf-8', errors='ignore')
    if use_mongo:
        users_collection.update_one({"username": doc["username"]}, {"$set": doc}, upsert=True)
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            data = f.read() or "{}"
            users = json_safe_loads(data)
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(json_safe_dumps(users))

# ---------- 인증 (회원가입 / 로그인) ----------
def sign_up(username, password):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."
    if get_user_record(username):
        return False, "이미 존재하는 사용자입니다."
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    # For local JSON, store as decoded string to avoid bytes issues
    store_password = hashed if use_mongo else hashed.decode('utf-8', errors='ignore')
    user_doc = {
        "username": username,
        "password": store_password,
        "cash": DEFAULT_CASH,
        "credit": DEFAULT_CREDIT,
        "holdings": {},
        "buy_prices": {},
        "logbook": [],
        "trade_count": 0,
        "goal": None,
        "goal_reached": False,
        "created_at": datetime.utcnow().isoformat()
    }
    save_user_record(user_doc)
    return True, "회원가입 완료. 로그인 해주세요."

def log_in(username, password):
    user = get_user_record(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None
    stored = user.get("password")
    # normalize stored to bytes for bcrypt.checkpw
    if isinstance(stored, str):
        try:
            stored_bytes = stored.encode('utf-8')
        except Exception:
            stored_bytes = stored
    else:
        stored_bytes = stored
    try:
        if bcrypt.checkpw(password.encode(), stored_bytes):
            return True, "로그인 성공", user
        else:
            return False, "비밀번호가 틀렸습니다.", None
    except Exception as e:
        return False, f"로그인 오류: {e}", None

# ---------- 주식 데이터 로직 ----------
@st.cache_data(ttl=3600)
def fetch_top_gainers(min_gain=0.0, top_n=20):
    tickers = list(ticker_to_name.keys())
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    try:
        data = yf.download(
            tickers,
            start=start_date.strftime('%Y-%m-%d'),
            end=end_date.strftime('%Y-%m-%d'),
            group_by='ticker',
            progress=False,
        )
    except Exception as e:
        st.warning(f"yfinance 다운로드 실패: {e}")
        return {}
    gainers = {}
    for ticker in tickers:
        try:
            df = data[ticker]
            if df.shape[0] < 2:
                continue
            prev_close = float(df['Close'].iloc[-2])
            last_close = float(df['Close'].iloc[-1])
            if prev_close == 0:
                continue
            gain = (last_close - prev_close) / prev_close
            gainers[ticker] = {'gain': gain, 'price': int(last_close)}
        except Exception:
            continue
    sorted_items = sorted(gainers.items(), key=lambda kv: kv[1]['gain'], reverse=True)[:top_n]
    return {k: v['price'] for k, v in sorted_items}

def initialize_game_state(user):
    user.setdefault("cash", DEFAULT_CASH)
    user.setdefault("credit", DEFAULT_CREDIT)
    user.setdefault("holdings", {})
    user.setdefault("buy_prices", {})
    user.setdefault("logbook", [])
    user.setdefault("trade_count", 0)
    user.setdefault("goal", None)
    user.setdefault("goal_reached", False)
    for name in ticker_to_name.values():
        user["holdings"].setdefault(name, 0)
        user["buy_prices"].setdefault(name, [])
    return user

# ---------- 시뮬레이터 유틸 ----------
def record_trade(user, action, name, qty, price):
    entry = {
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "name": name,
        "qty": qty,
        "price": price
    }
    user.setdefault("logbook", []).append(entry)
    user["trade_count"] = user.get("trade_count", 0) + 1

def buy_stock(user, name, qty, price):
    cost = qty * price * (1 + FEE_RATE)
    if cost > user.get("cash", 0):
        return False, "현금이 부족합니다."
    user["cash"] -= int(cost)
    user["holdings"][name] = user["holdings"].get(name, 0) + qty
    user["buy_prices"].setdefault(name, []).append(price)
    record_trade(user, "BUY", name, qty, price)
    save_user_record(user)
    return True, "매수 완료"

def sell_stock(user, name, qty, price):
    if user["holdings"].get(name, 0) < qty:
        return False, "보유 수량 부족"
    revenue = qty * price * (1 - FEE_RATE)
    user["holdings"][name] -= qty
    bp = user["buy_prices"].get(name, [])
    for _ in range(min(qty, len(bp))):
        bp.pop(0)
    user["cash"] += int(revenue)
    record_trade(user, "SELL", name, qty, price)
    save_user_record(user)
    return True, "매도 완료"

# ---------- Streamlit 페이지 ----------
def show_simulator(user):
    st.sidebar.title(f"안녕하세요, {user['username']}")
    if st.sidebar.button("로그아웃"):
        st.session_state.pop("logged_in_user", None)
        st.experimental_rerun()

    st.title("📈 가상 주식 시뮬레이터 - Render 배포용")
    # Fetch tickers/prices (cached)
    tickers_prices = fetch_top_gainers()
    if not tickers_prices:
        tickers_prices = {
            "005930.KS": 83000,
            "000660.KS": 195000,
            "373220.KQ": 370000,
            "005380.KS": 260000,
            "035420.KS": 170000,
            "035720.KS": 59000,
            "012330.KS": 210000,
        }
    stocks = { ticker_to_name.get(t): p for t,p in tickers_prices.items() }

    col1, col2 = st.columns([2,1])
    with col1:
        st.subheader("시장 목록")
        df = pd.DataFrame([{"종목":k, "현재가":v} for k,v in stocks.items()])
        st.dataframe(df, use_container_width=True)

        name = st.selectbox("종목 선택", list(stocks.keys()))
        price_now = stocks[name]
        qty = st.number_input("수량", min_value=1, step=1, value=1)
        buy_col, sell_col = st.columns(2)
        with buy_col:
            if st.button("매수"):
                ok, msg = buy_stock(user, name, qty, price_now)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
        with sell_col:
            if st.button("매도"):
                ok, msg = sell_stock(user, name, qty, price_now)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

    with col2:
        st.subheader("계좌 정보")
        st.info(f"현금: {user.get('cash',0):,}원")
        st.write("보유 종목")
        holdings = user.get("holdings", {})
        holding_df = pd.DataFrame([{"종목":k, "수량":v, "현재가": stocks.get(k, 0), "평가액": v*stocks.get(k,0)} for k,v in holdings.items() if v>0])
        if not holding_df.empty:
            st.dataframe(holding_df, use_container_width=True)
            total_eval = holding_df["평가액"].sum()
            st.write(f"총 평가액: {int(total_eval):,}원")
        st.subheader("로그북")
        for entry in user.get("logbook", [])[::-1][:10]:
            st.write(f"{entry['time']} - {entry['action']} {entry['name']} x{entry['qty']} @ {entry['price']:,}원")

    save_user_record(user)

def main():
    if "logged_in_user" in st.session_state:
        user = st.session_state["logged_in_user"]
        refreshed = get_user_record(user["username"])
        if refreshed:
            user.update(refreshed)
        user = initialize_game_state(user)
        st.session_state["logged_in_user"] = user
        show_simulator(user)
        return

    st.title("📈 가상 주식 시뮬레이터 - 로그인")

    choice = st.radio("선택", ["로그인", "회원가입"])

    username = st.text_input("사용자명")
    password = st.text_input("비밀번호", type="password")

    if choice == "회원가입" and st.button("회원가입"):
        ok, msg = sign_up(username, password)
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    if choice == "로그인" and st.button("로그인"):
        ok, msg, user = log_in(username, password)
        if ok:
            st.success(msg)
            user = initialize_game_state(user)
            st.session_state["logged_in_user"] = user
            st.experimental_rerun()
        else:
            st.error(msg)

if __name__ == '__main__':
    main()
