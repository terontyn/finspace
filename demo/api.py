"""Read-only synthetic API adapter for the existing Next.js UI. No database or integrations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json
from datetime import datetime, timezone

STAMP = datetime.now(timezone.utc).isoformat()
USER = dict(id="00000000-0000-4000-8000-000000000001", email="demo@example.invalid", display_name="Демо пользователь", locale="ru", timezone="UTC", is_active=True, version=1)
WORKSPACE = dict(id="00000000-0000-4000-8000-000000000002", name="Вымышленный семейный бюджет", base_currency="RUB", timezone="UTC", owner_user_id=USER["id"], version=1)
ACCOUNTS = [dict(id=f"00000000-0000-4000-8000-{i:012d}", name=name, account_type=kind, currency="RUB", institution="Демо организация", opening_balance=balance, opening_balance_at=STAMP, credit_limit=None, description="Синтетический пример", is_archived=False, version=1, created_at=STAMP, updated_at=STAMP) for i, name, kind, balance in [(10,"Демо карта","debit_card","45000.0000"),(11,"Демо накопления","savings","120000.0000")]]
CATEGORIES = [dict(id=f"00000000-0000-4000-8000-{i:012d}", name=name, parent_id=None, category_type=kind, color=color, icon=None, sort_order=i, is_archived=False, version=1, created_at=STAMP, updated_at=STAMP, children=[]) for i,name,kind,color in [(20,"Демо доход","income","#16a085"),(21,"Демо покупки","expense","#d97f35")]]
TRANSACTIONS = [dict(id=f"00000000-0000-4000-8000-{i:012d}", occurred_at=STAMP, transaction_type=kind, amount=amount, currency="RUB", account={"id":ACCOUNTS[0]["id"],"name":ACCOUNTS[0]["name"]}, target_account=None, category={"id":CATEGORIES[0 if kind=="income" else 1]["id"],"name":CATEGORIES[0 if kind=="income" else 1]["name"]}, payee=None, counterparty="Демо контрагент", description=description, comment=None, status="confirmed", source="manual", related_transaction_id=None, external_id=None, splits=[], version=1, created_at=STAMP, updated_at=STAMP) for i,kind,amount,description in [(30,"income","50000.0000","Вымышленное поступление"),(31,"expense","3250.0000","Вымышленная покупка"),(32,"expense","1800.0000","Вымышленная услуга")]]

def page(items, query):
    limit=int(query.get("limit",[50])[0]);offset=int(query.get("offset",[0])[0])
    return {"items":items[offset:offset+limit],"meta":{"limit":limit,"offset":offset,"total":len(items)}}

class Handler(BaseHTTPRequestHandler):
    def respond(self,status,data=None):
        raw=json.dumps(data,ensure_ascii=False).encode() if data is not None else b""
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store")
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        url=urlparse(self.path);path=url.path;query=parse_qs(url.query)
        if path=="/api/v1/auth/me":return self.respond(200,{"user":USER,"workspace":WORKSPACE,"role":"viewer"})
        if path=="/api/v1/accounts/balances":return self.respond(200,[{"account_id":a["id"],"name":a["name"],"currency":"RUB","opening_balance":a["opening_balance"],"balance":balance} for a,balance in zip(ACCOUNTS,["89950.0000","120000.0000"])])
        if path=="/api/v1/accounts":return self.respond(200,page(ACCOUNTS,query))
        if path in ("/api/v1/categories","/api/v1/categories/tree"):return self.respond(200,CATEGORIES if path.endswith('/tree') else page(CATEGORIES,query))
        if path=="/api/v1/transactions":return self.respond(200,page(TRANSACTIONS,query))
        if path=="/api/v1/financial-summary":return self.respond(200,{"groups":[{"currency":"RUB","income":"50000.0000","expense":"5050.0000","net_cashflow":"44950.0000","transfer_volume":"0.0000","transactions_count":3}]})
        if path=="/api/v1/payees":return self.respond(200,page([],query))
        if path in ("/api/v1/health","/api/v1/health/ready"):return self.respond(200,{"status":"demo","production_backend":False})
        return self.respond(404,{"error":{"code":"DEMO_UNSUPPORTED","message":"Этот экран не входит в read-only demo. Запустите основной backend для полного функционала."}})
    def do_POST(self):
        if self.path=="/api/v1/auth/refresh":return self.respond(200,{"access_token":"demo-session-not-a-credential","expires_in":3600,"user":USER,"workspace":WORKSPACE})
        if self.path.startswith("/api/v1/auth/logout"):return self.respond(204)
        self.respond(405,{"error":{"code":"DEMO_READ_ONLY","message":"Демо только для просмотра. Основной backend не подключён."}})
    do_PATCH=do_POST
    do_PUT=do_POST
    do_DELETE=do_POST

if __name__=="__main__":
    print("Read-only synthetic API: http://127.0.0.1:8790",flush=True)
    ThreadingHTTPServer(("127.0.0.1",8790),Handler).serve_forever()
