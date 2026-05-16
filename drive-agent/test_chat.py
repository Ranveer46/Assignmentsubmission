import requests
try:
    resp = requests.post("http://127.0.0.1:8000/chat", json={"session_id": "test_session", "message": "Show QR codes"})
    print(resp.json())
except Exception as e:
    print(e)
