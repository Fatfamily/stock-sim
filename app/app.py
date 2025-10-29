import os
import streamlit as st
from pymongo import MongoClient
import bcrypt
import random
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import json

# ----------------- 설정 -----------------
st.set_page_config(page_title="📈 가상 주식 시뮬레이터", layout="wide")
DEFAULT_CASH = 1000000
DEFAULT_CREDIT = 100
FEE_RATE = 0.0003
TRADE_LIMIT_BEFORE_PENALTY = 5

# 티커-한글명
ticker_to_name = {
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "373220.KQ": "LG에너지솔루션",
    "005380.KS": "현대차", "035420.KS": "NAVER", "035720.KS": "카카오",
    "012330.KS": "현대모비스", "051910.KS": "LG화학", "068270.KS": "셀트리온",
    "207940.KQ": "삼성바이오로직스", "055550.KS": "신한지주", "105560.KS": "KB금융",
    "005490.KS": "POSCO홀딩스", "096770.KS": "SK이노베이션", "003550.KS": "LG",
    "015760.KS": "한국전력", "086790.KQ": "하나금융지주", "034020.KS": "두산에너빌리티",
    "066570.KS": "LG전자", "028260.KS": "삼성물산",
}
name_to_ticker = {v:k for k,v in ticker_to_name.items()}

# ----------------- DB 설정 -----------------
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
        st.warning(f"MongoDB 연결 실패, 로컬 파일로 대체합니다. ({e})")

LOCAL_USERS_FILE = os.path.join(os.getcwd(), "users_db.json")
if not use_mongo and not os.path.exists(LOCAL_USERS_FILE):
    with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
        f.write("{}")

def json_loads_safe(s):
    try:
        return json.loads(s)
    except:
        return {}

def json_dumps_safe(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except:
        return "{}"

# ----------------- 유저 기록 -----------------
def get_user_record(username):
    if use_mongo:
        return users_collection.find_one({"username": username})
    else:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as f:
            data = f.read() or "{}"
            users = json_loads_safe(data)
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
            users = json_loads_safe(data)
        users[doc["username"]] = doc
        with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as f:
            f.write(json_dumps_safe(users))

# ----------------- 인증 -----------------
def sign_up(username, password):
    if not username or not password:
        return False, "아이디와 비밀번호를 입력하세요."
    if get_user_record(username):
        return False, "이미 존재하는 사용자입니다."
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    store_password = hashed if use_mongo else hashed.decode("utf-8", errors="ignore")
    user_doc = {
        "username": username,
        "password": store_password,
        "cash": DEFAULT_CASH,
        "credit": DEFAULT_CREDIT,
        "holdings": {name:0 for name in ticker_to_name.values()},
        "buy_prices": {name:[] for name in ticker_to_name.values()},
        "logbook": [],
        "trade_count": 0,
        "goal": None,
        "goal_reached": False,
        "created_at": datetime.utcnow().isoformat()
    }
    save_user_record(user_doc)
    return True, "회원가입 완료. 로그인 해주세요."

def log_in(username, password):
    if username=="admin" and password=="1q2w3e4r":
        return True, "관리자 로그인", {"username":"admin"}
    user = get_user_record(username)
    if not user:
        return False, "사용자가 존재하지 않습니다.", None
    stored = user.get("password")
    if isinstance(stored, str):
        stored_bytes = stored.encode("utf-8")
    else:
        stored_bytes = stored
    if bcrypt.checkpw(password.encode(), stored_bytes):
        return True, "로그인 성공", user
    else:
        return False, "비밀번호가 틀렸습니다.", None

# ----------------- 주가 로직 -----------------
last_prices = {}
last_update_date = None

def fetch_market_prices():
    global last_prices, last_update_date
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if last_update_date != today_str:
        # 매일 10시에 yfinance에서 가져오기
        try:
            end_date = now
            start_date = end_date - timedelta(days=7)
            data = yf.download(
                list(ticker_to_name.keys()),
                start=start_date.strftime('%Y-%m-%d'),
                end=end_date.strftime('%Y-%m-%d'),
                group_by='ticker',
                progress=False
            )
            for ticker in ticker_to_name.keys():
                df = data.get(ticker)
                if df is not None and df.shape[0]>1:
                    last_close = float(df['Close'].iloc[-1])
                    last_prices[ticker] = last_close
        except:
            # 실패시 기본값
            last_prices = {t: random.randint(50000,300000) for t in ticker_to_name.keys()}
        last_update_date = today_str
    else:
        # 랜덤 ±1000원 변경
        for t in last_prices:
            last_prices[t] = max(1, last_prices[t]+random.randint(-1000,1000))
    return {ticker_to_name[t]: int(p) for t,p in last_prices.items()}

# ----------------- 거래 -----------------
def record_trade(user, action, name, qty, price):
    entry = {"time": datetime.utcnow().isoformat(), "action":action, "name":name, "qty":qty, "price":price}
    user.setdefault("logbook", []).append(entry)
    user["trade_count"] = user.get("trade_count",0)+1

def buy_stock(user,name,qty,price):
    cost = int(qty*price*(1+FEE_RATE))
    if user["cash"]<cost:
        return False,"현금 부족"
    user["cash"] -= cost
    user["holdings"][name] += qty
    user["buy_prices"].setdefault(name,[]).append(price)
    record_trade(user,"BUY",name,qty,price)
    save_user_record(user)
    return True,"매수 완료"

def sell_stock(user,name,qty,price):
    if user["holdings"].get(name,0)<qty:
        return False,"보유 수량 부족"
    revenue = int(qty*price*(1-FEE_RATE))
    user["holdings"][name]-=qty
    bp = user["buy_prices"].get(name,[])
    for _ in range(min(qty,len(bp))): bp.pop(0)
    user["cash"]+=revenue
    record_trade(user,"SELL",name,qty,price)
    save_user_record(user)
    return True,"매도 완료"

# ----------------- 관리자 기능 -----------------
def admin_panel():
    st.subheader("관리자 기능")
    action = st.radio("작업 선택", ["회원 생성","회원 삭제","순위 확인"])
    if action=="회원 생성":
        uname = st.text_input("새 사용자명")
        pwd = st.text_input("비밀번호", type="password")
        if st.button("생성"):
            ok,msg = sign_up(uname,pwd)
            if ok: st.success(msg)
            else: st.error(msg)
    elif action=="회원 삭제":
        users_list = []
        if use_mongo:
            users_list = [u["username"] for u in users_collection.find({})]
        else:
            with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                users_list = list(json_loads_safe(f.read()).keys())
        del_user = st.selectbox("삭제할 사용자 선택", users_list)
        if st.button("삭제"):
            if use_mongo:
                users_collection.delete_one({"username":del_user})
            else:
                with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                    data=json_loads_safe(f.read())
                data.pop(del_user,None)
                with open(LOCAL_USERS_FILE,"w",encoding="utf-8") as f:
                    f.write(json_dumps_safe(data))
            st.success(f"{del_user} 삭제 완료")
    else: # 순위 확인
        all_users=[]
        if use_mongo:
            all_users = list(users_collection.find({}))
        else:
            with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                all_users = list(json_loads_safe(f.read()).values())
        ranking = sorted(all_users,key=lambda u:u.get("cash",0),reverse=True)
        df = pd.DataFrame([{"순위":i+1,"사용자":u["username"],"현금":u.get("cash",0)} for i,u in enumerate(ranking)])
        st.dataframe(df,use_container_width=True)

# ----------------- 시뮬레이터 -----------------
def show_simulator(user):
    st.sidebar.title(f"{user['username']} 님")
    if st.sidebar.button("로그아웃"):
        st.session_state.pop("logged_in_user",None)
        st.experimental_rerun()
    prices = fetch_market_prices()
    col1,col2 = st.columns([2,1])
    with col1:
        st.subheader("시장 목록")
        df = pd.DataFrame([{"종목":k,"현재가":v} for k,v in prices.items()])
        st.dataframe(df,use_container_width=True)
        name = st.selectbox("종목 선택",list(prices.keys()))
        price_now = prices[name]
        qty = st.number_input("수량",1,100000,1)
        buy_col,sell_col = st.columns(2)
        with buy_col:
            if st.button("매수"):
                ok,msg = buy_stock(user,name,qty,price_now)
                st.success(msg) if ok else st.error(msg)
        with sell_col:
            if st.button("매도"):
                ok,msg = sell_stock(user,name,qty,price_now)
                st.success(msg) if ok else st.error(msg)
    with col2:
        st.subheader("계좌")
        st.info(f"현금: {user.get('cash',0):,}원")
        holding_df = pd.DataFrame([{"종목":k,"수량":v,"현재가":prices[k],"평가액":v*prices[k]} for k,v in user.get("holdings",{}).items() if v>0])
        if not holding_df.empty:
            st.dataframe(holding_df,use_container_width=True)
            st.write(f"총 평가액: {holding_df['평가액'].sum():,}원")
        st.subheader("최근 거래")
        for e in user.get("logbook",[])[::-1][:10]:
            st.write(f"{e['time']} - {e['action']} {e['name']} x{e['qty']} @ {e['price']:,}원")

# ----------------- 메인 -----------------
def main():
    st.title("📈 가상 주식 시뮬레이터")
    choice = st.radio("선택",["로그인","회원가입","순위보기"])
    username = st.text_input("사용자명")
    password = st.text_input("비밀번호", type="password")
    if choice=="회원가입" and st.button("회원가입"):
        ok,msg = sign_up(username,password)
        st.success(msg) if ok else st.error(msg)
    elif choice=="로그인" and st.button("로그인"):
        ok,msg,user = log_in(username,password)
        if ok:
            st.success(msg)
            if username=="admin":
                admin_panel()
            else:
                st.session_state["logged_in_user"]=user
                st.experimental_rerun()
        else:
            st.error(msg)
    elif choice=="순위보기":
        all_users=[]
        if use_mongo:
            all_users = list(users_collection.find({}))
        else:
            with open(LOCAL_USERS_FILE,"r",encoding="utf-8") as f:
                all_users = list(json_loads_safe(f.read()).values())
        ranking = sorted(all_users,key=lambda u:u.get("cash",0),reverse=True)
        df = pd.DataFrame([{"순위":i+1,"사용자":u["username"],"현금":u.get("cash",0)} for i,u in enumerate(ranking)])
        st.dataframe(df,use_container_width=True)
    elif "logged_in_user" in st.session_state:
        show_simulator(st.session_state["logged_in_user"])

if __name__=="__main__":
    main()
