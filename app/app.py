# app/app.py
import os
import streamlit as st
from pymongo import MongoClient
import bcrypt
import random
from datetime import datetime, timedelta, time
import yfinance as yf
import pandas as pd

# ---------- 설정 ----------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")
DEFAULT_CASH = 1000000
FEE_RATE = 0.0003

# ---------- 티커 설정 ----------
ticker_to_name = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "373220.KQ": "LG에너지솔루션",
    "005380.KS": "현대차",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
}
name_to_ticker = {v: k for k, v in ticker_to_name.items()}

# ---------- DB 설정 ----------
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
        st.warning(f"MongoDB 연결 실패, 로컬 JSON 사용 ({e})")
        use_mongo = False

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo:
    if not os.path.exists(LOCAL_USERS_FILE):
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write("{}")

def json_safe_loads(s):
    import json
    try:
        return json.loads(s)
    except:
        return {}

def json_safe_dumps(obj):
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except:
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
    doc = dict(user_doc)
    if isinstance(doc.get("password"), (bytes, bytearray)):
        doc["password"] = doc["password"].decode("utf-8", errors="ignore")
    if use_mongo:
        users_collection.update_one({"username": doc["username"]}, {"$set": doc}, upsert=True)
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            data = f.read() or "{}"
            users = json_safe_loads(data)
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(json_safe_dumps(users))

# ---------- 유저 인증 ----------
def sign_up(username, password):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."
    if get_user_record(username):
        return False, "이미 존재하는 사용자입니다."
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    user_doc = {
        "username": username,
        "password": hashed.decode('utf-8'),
        "cash": DEFAULT_CASH,
        "holdings": {name: 0 for name in ticker_to_name.values()},
        "buy_prices": {name: [] for name in ticker_to_name.values()},
        "logbook": [],
        "created_at": datetime.utcnow().isoformat()
    }
    save_user_record(user_doc)
    return True, "회원가입 완료. 로그인 해주세요."

def log_in(username, password):
    user = get_user_record(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None
    stored_pw = user["password"].encode("utf-8")
    if bcrypt.checkpw(password.encode(), stored_pw):
        return True, "로그인 성공", user
    else:
        return False, "비밀번호가 틀렸습니다.", None

# ---------- 주식 데이터 ----------
STOCK_DATA = {}
LAST_UPDATE = None

def fetch_stock_prices():
    global STOCK_DATA, LAST_UPDATE
    now = datetime.now()
    # 매일 10시에 실제 주가 가져오기
    if LAST_UPDATE is None or (LAST_UPDATE.date() != now.date() and now.time() >= time(10,0)):
        tickers = list(ticker_to_name.keys())
        data = yf.download(tickers, period="2d", progress=False, group_by='ticker')
        for t in tickers:
            try:
                df = data[t]
                price = int(df['Close'].iloc[-1])
                STOCK_DATA[t] = price
            except:
                STOCK_DATA[t] = STOCK_DATA.get(t, 100000)
        LAST_UPDATE = now
    else:
        # 그 이후는 ±1000원 랜덤
        for t in STOCK_DATA.keys():
            STOCK_DATA[t] += random.randint(-1000, 1000)
            if STOCK_DATA[t] < 1000:
                STOCK_DATA[t] = 1000
    return {ticker_to_name[t]: p for t,p in STOCK_DATA.items()}

# ---------- 거래 ----------
def buy_stock(user, name, qty, price):
    cost = int(qty * price * (1 + FEE_RATE))
    if user["cash"] < cost:
        return False, "현금 부족"
    user["cash"] -= cost
    user["holdings"][name] += qty
    user["buy_prices"][name].append(price)
    user["logbook"].append(f"{datetime.now().strftime('%H:%M')} BUY {name} x{qty} @ {price}")
    save_user_record(user)
    return True, "매수 완료"

def sell_stock(user, name, qty, price):
    if user["holdings"].get(name,0) < qty:
        return False, "보유 수량 부족"
    revenue = int(qty * price * (1 - FEE_RATE))
    user["cash"] += revenue
    user["holdings"][name] -= qty
    for _ in range(min(qty, len(user["buy_prices"][name]))):
        user["buy_prices"][name].pop(0)
    user["logbook"].append(f"{datetime.now().strftime('%H:%M')} SELL {name} x{qty} @ {price}")
    save_user_record(user)
    return True, "매도 완료"

# ---------- 관리자 기능 ----------
ADMIN_ID = "admin"
ADMIN_PW = "1q2w3e4r"

def admin_dashboard():
    st.title("관리자 대시보드")
    choice = st.radio("선택", ["회원 생성", "회원 삭제", "순위 보기"])
    
    if choice == "회원 생성":
        uname = st.text_input("아이디")
        upw = st.text_input("비밀번호", type="password")
        if st.button("생성"):
            ok, msg = sign_up(uname, upw)
            if ok: st.success(msg)
            else: st.error(msg)
            
    elif choice == "회원 삭제":
        uname = st.text_input("삭제할 사용자명")
        if st.button("삭제"):
            if get_user_record(uname):
                if use_mongo:
                    users_collection.delete_one({"username": uname})
                else:
                    with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                        data = f.read() or "{}"
                        users = json_safe_loads(data)
                    users.pop(uname,None)
                    with open(LOCAL_USERS_FILE,"w",encoding="utf-8") as f:
                        f.write(json_safe_dumps(users))
                st.success(f"{uname} 삭제 완료")
            else:
                st.error("사용자 없음")
    
    elif choice == "순위 보기":
        if use_mongo:
            all_users = list(users_collection.find({}))
        else:
            with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                all_users = [v for v in json_safe_loads(f.read()).values()]
        all_users.sort(key=lambda x: x["cash"], reverse=True)
        df = pd.DataFrame([{"사용자": u["username"], "현금": u["cash"]} for u in all_users])
        st.dataframe(df)

# ---------- 시뮬레이터 ----------
def simulator(user):
    st.title(f"{user['username']}님의 가상 주식 시뮬레이터")
    stocks = fetch_stock_prices()
    
    col1, col2 = st.columns([2,1])
    
    with col1:
        st.subheader("주식 목록")
        df = pd.DataFrame([{"종목":k,"현재가":v} for k,v in stocks.items()])
        st.dataframe(df,use_container_width=True)
        
        name = st.selectbox("종목 선택", list(stocks.keys()))
        price_now = stocks[name]
        qty = st.number_input("수량", min_value=1, step=1, value=1)
        
        buy_col, sell_col = st.columns(2)
        with buy_col:
            if st.button("매수"):
                ok,msg = buy_stock(user,name,qty,price_now)
                if ok: st.success(msg)
                else: st.error(msg)
        with sell_col:
            if st.button("매도"):
                ok,msg = sell_stock(user,name,qty,price_now)
                if ok: st.success(msg)
                else: st.error(msg)
    
    with col2:
        st.subheader("계좌 정보")
        st.write(f"현금: {user['cash']:,}원")
        holding_df = pd.DataFrame([{"종목":k,"수량":v,"평가액":v*stocks.get(k,0)} for k,v in user["holdings"].items() if v>0])
        if not holding_df.empty:
            st.dataframe(holding_df,use_container_width=True)
            st.write(f"총 평가액: {holding_df['평가액'].sum():,}원")
        st.subheader("로그북")
        for log in user["logbook"][::-1][:10]:
            st.write(log)

# ---------- 메인 ----------
def main():
    if "logged_in_user" not in st.session_state:
        st.session_state["logged_in_user"] = None
    
    if st.session_state["logged_in_user"]:
        user = st.session_state["logged_in_user"]
        if user["username"] == ADMIN_ID:
            admin_dashboard()
        else:
            simulator(user)
        if st.button("로그아웃"):
            st.session_state["logged_in_user"] = None
            st.experimental_rerun()
        return
    
    st.title("📈 가상 주식 시뮬레이터 로그인")
    choice = st.radio("선택", ["로그인","회원가입","순위 보기"])
    
    username = st.text_input("아이디")
    password = st.text_input("비밀번호", type="password")
    
    if choice == "회원가입" and st.button("회원가입"):
        ok,msg = sign_up(username,password)
        if ok: st.success(msg)
        else: st.error(msg)
    
    if choice == "로그인" and st.button("로그인"):
        # 관리자 로그인
        if username == ADMIN_ID and password == ADMIN_PW:
            st.success("관리자 로그인 성공")
            st.session_state["logged_in_user"] = {"username":ADMIN_ID}
            st.experimental_rerun()
        else:
            ok,msg,user = log_in(username,password)
            if ok:
                st.success(msg)
                st.session_state["logged_in_user"] = user
                st.experimental_rerun()
            else:
                st.error(msg)
    
    if choice == "순위 보기" and st.button("순위 확인"):
        if use_mongo:
            all_users = list(users_collection.find({}))
        else:
            with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                all_users = [v for v in json_safe_loads(f.read()).values()]
        all_users.sort(key=lambda x:x["cash"], reverse=True)
        df = pd.DataFrame([{"사용자":u["username"],"현금":u["cash"]} for u in all_users])
        st.dataframe(df)

if __name__ == "__main__":
    main()
