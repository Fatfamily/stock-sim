import os
import streamlit as st
from pymongo import MongoClient
import bcrypt
import random
from datetime import datetime, timedelta, time
import yfinance as yf
import pandas as pd
import altair as alt
import json

# -------------------- 설정 --------------------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")
DEFAULT_CASH = 1000000
FEE_RATE = 0.0003
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "1q2w3e4r"
PRICE_VARIATION_LIMIT = 1000  # 10시 이후 가격 변동 범위

# -------------------- 티커 --------------------
ticker_to_name = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "373220.KQ": "LG에너지솔루션",
    "005380.KS": "현대차",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
}
name_to_ticker = {v:k for k,v in ticker_to_name.items()}

# -------------------- DB 설정 --------------------
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

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo:
    if not os.path.exists(LOCAL_USERS_FILE):
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write("{}")

# -------------------- JSON Helper --------------------
def json_safe_loads(s):
    try:
        return json.loads(s)
    except Exception:
        return {}

def json_safe_dumps(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return "{}"

# -------------------- 사용자 데이터 --------------------
def get_user_record(username):
    if use_mongo:
        return users_collection.find_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = json_safe_loads(f.read() or "{}")
        return users.get(username)

def save_user_record(user_doc):
    doc = dict(user_doc)
    if isinstance(doc.get("password"), (bytes, bytearray)):
        doc["password"] = doc["password"].decode('utf-8', errors='ignore')
    if use_mongo:
        users_collection.update_one({"username": doc["username"]}, {"$set": doc}, upsert=True)
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            users = json_safe_loads(f.read() or "{}")
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(json_safe_dumps(users))

# -------------------- 인증 --------------------
def create_user(username, password, cash=DEFAULT_CASH):
    if get_user_record(username):
        return False, "이미 존재하는 사용자입니다."
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    store_password = hashed.decode('utf-8', errors='ignore')
    user_doc = {
        "username": username,
        "password": store_password,
        "cash": cash,
        "holdings": {name:0 for name in ticker_to_name.values()},
        "buy_prices": {name:[] for name in ticker_to_name.values()},
        "logbook": [],
        "trade_count": 0,
        "goal": None,
        "goal_reached": False,
        "created_at": datetime.utcnow().isoformat()
    }
    save_user_record(user_doc)
    return True, "회원 생성 완료."

def verify_login(username, password):
    user = get_user_record(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None
    stored = user.get("password")
    stored_bytes = stored.encode('utf-8') if isinstance(stored,str) else stored
    if bcrypt.checkpw(password.encode(), stored_bytes):
        return True, "로그인 성공", user
    else:
        return False, "비밀번호가 틀렸습니다.", None

# -------------------- 주식 데이터 --------------------
price_cache = {}
last_yfinance_update = None

def fetch_prices():
    global price_cache, last_yfinance_update
    now = datetime.now()
    if last_yfinance_update is None or now.time() >= time(10,0) and (last_yfinance_update.date() != now.date() or last_yfinance_update.time() < time(10,0)):
        tickers = list(ticker_to_name.keys())
        data = yf.download(tickers, period='2d', progress=False, group_by='ticker')
        for t in tickers:
            try:
                df = data[t]
                price_cache[ticker_to_name[t]] = int(df['Close'].iloc[-1])
            except:
                price_cache[ticker_to_name[t]] = random.randint(10000,100000)
        last_yfinance_update = now
    else:
        # 10시 이후 ±1000원 랜덤 변동
        for name in price_cache:
            price_cache[name] += random.randint(-PRICE_VARIATION_LIMIT, PRICE_VARIATION_LIMIT)
            if price_cache[name] < 1000:
                price_cache[name] = 1000
    return price_cache

# -------------------- 거래 --------------------
def record_trade(user, action, name, qty, price):
    entry = {
        "time": datetime.utcnow().isoformat(),
        "action": action,
        "name": name,
        "qty": qty,
        "price": price
    }
    user.setdefault("logbook",[]).append(entry)
    user["trade_count"] += 1
    save_user_record(user)

def buy_stock(user, name, qty, price):
    cost = qty*price*(1+FEE_RATE)
    if cost > user.get("cash",0):
        return False, "현금이 부족합니다."
    user["cash"] -= int(cost)
    user["holdings"][name] += qty
    user["buy_prices"][name].append(price)
    record_trade(user,"BUY",name,qty,price)
    return True, "매수 완료"

def sell_stock(user, name, qty, price):
    if user["holdings"].get(name,0) < qty:
        return False, "보유 수량 부족"
    revenue = qty*price*(1-FEE_RATE)
    user["holdings"][name] -= qty
    bp = user["buy_prices"].get(name,[])
    for _ in range(min(qty,len(bp))):
        bp.pop(0)
    user["cash"] += int(revenue)
    record_trade(user,"SELL",name,qty,price)
    return True, "매도 완료"

# -------------------- Streamlit 페이지 --------------------
def show_simulator(user):
    st.sidebar.title(f"안녕하세요, {user['username']}")
    if st.sidebar.button("로그아웃"):
        st.session_state.pop("logged_in_user",None)
        st.experimental_rerun()

    prices = fetch_prices()
    col1,col2 = st.columns([2,1])
    with col1:
        st.subheader("시장 목록")
        df = pd.DataFrame([{"종목":k,"현재가":v} for k,v in prices.items()])
        st.dataframe(df,use_container_width=True)

        name = st.selectbox("종목 선택", list(prices.keys()))
        qty = st.number_input("수량",1,1000,1)
        buy_col,sell_col = st.columns(2)
        with buy_col:
            if st.button("매수"):
                ok,msg = buy_stock(user,name,qty,prices[name])
                st.success(msg) if ok else st.error(msg)
        with sell_col:
            if st.button("매도"):
                ok,msg = sell_stock(user,name,qty,prices[name])
                st.success(msg) if ok else st.error(msg)

    with col2:
        st.subheader("계좌 정보")
        st.info(f"현금: {user['cash']:,}원")
        holdings = user.get("holdings",{})
        holding_df = pd.DataFrame([{"종목":k,"수량":v,"현재가":prices.get(k,0),"평가액":v*prices.get(k,0)} for k,v in holdings.items() if v>0])
        if not holding_df.empty:
            st.dataframe(holding_df,use_container_width=True)
            total_eval = holding_df['평가액'].sum()
            st.write(f"총 평가액: {total_eval:,}원")

        st.subheader("로그북")
        for entry in user.get("logbook",[])[::-1][:10]:
            st.write(f"{entry['time']} - {entry['action']} {entry['name']} x{entry['qty']} @ {entry['price']:,}원")

# -------------------- 관리자 페이지 --------------------
def admin_page():
    st.title("⚙️ 관리자 페이지")
    choice = st.radio("선택", ["회원 생성","회원 삭제","보유현금 순위"])

    if choice=="회원 생성":
        new_user = st.text_input("새 사용자명")
        new_pw = st.text_input("비밀번호",type="password")
        if st.button("생성"):
            ok,msg = create_user(new_user,new_pw)
            st.success(msg) if ok else st.error(msg)

    elif choice=="회원 삭제":
        users = []
        if use_mongo:
            users = [u['username'] for u in users_collection.find() if u['username'] != ADMIN_USERNAME]
        else:
            with open(LOCAL_USERS_FILE,'r',encoding='utf-8') as f:
                users = [k for k in json_safe_loads(f.read()).keys() if k != ADMIN_USERNAME]
        del_user = st.selectbox("삭제할 회원 선택", users)
        if st.button("삭제"):
            if use_mongo:
                users_collection.delete_one({"username":del_user})
            else:
                with open(LOCAL_USERS_FILE,'r',encoding='utf-8') as f:
                    data = json_safe_loads(f.read())
                data.pop(del_user,None)
                with open(LOCAL_USERS_FILE,'w',encoding='utf-8') as f:
                    f.write(json_safe_dumps(data))
            st.success(f"{del_user} 삭제 완료")

    elif choice=="보유현금 순위":
        all_users = []
        if use_mongo:
            all_users = list(users_collection.find())
        else:
            with open(LOCAL_USERS_FILE,'r',encoding='utf-8') as f:
                all_users = list(json_safe_loads(f.read()).values())
        df = pd.DataFrame([{'사용자':u['username'],'현금':u['cash']} for u in all_users])
        df = df.sort_values('현금',ascending=False)
        st.dataframe(df,use_container_width=True)
        chart = alt.Chart(df).mark_bar().encode(x='사용자',y='현금')
        st.altair_chart(chart,use_container_width=True)

# -------------------- 메인 --------------------
def main():
    if 'logged_in_user' in st.session_state:
        user = st.session_state['logged_in_user']
        if user['username']==ADMIN_USERNAME:
            admin_page()
        else:
            show_simulator(user)
        return

    st.title("📈 가상 주식 시뮬레이터 로그인")
    choice = st.radio("선택", ["로그인","회원가입"])
    username = st.text_input("사용자명")
    password = st.text_input("비밀번호",type="password")

    if choice=="회원가입" and st.button("회원가입"):
        ok,msg = create_user(username,password)
        st.success(msg) if ok else st.error(msg)

    if choice=="로그인" and st.button("로그인"):
        if username==ADMIN_USERNAME and password==ADMIN_PASSWORD:
            st.session_state['logged_in_user']={'username':ADMIN_USERNAME}
            st.experimental_rerun()
        ok,msg,user = verify_login(username,password)
        st.success(msg) if ok else st.error(msg)
        if ok:
            st.session_state['logged_in_user']=user
            st.experimental_rerun()

if __name__=='__main__':
    main()
