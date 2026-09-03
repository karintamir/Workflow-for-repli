from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
HUBSPOT_API_BASE = "https://api.hubapi.com"

CLIENT_ID = os.getenv("HUBSPOT_CLIENT_ID")
CLIENT_SECRET = os.getenv("HUBSPOT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("HUBSPOT_REDIRECT_URI")

@app.route("/oauth/token", methods=["POST"])
def token():
    data = request.json
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": data.get("code")
    }
    headers = { "Content-Type": "application/x-www-form-urlencoded" }
    response = requests.post(HUBSPOT_TOKEN_URL, data=payload, headers=headers)
    return jsonify(response.json()), response.status_code

@app.route("/hubspot/<path:path>", methods=["GET", "POST"])
def proxy(path):
    access_token = request.headers.get("Authorization")
    url = f"{HUBSPOT_API_BASE}/{path}"
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json"
    }
    if request.method == "GET":
        response = requests.get(url, headers=headers, params=request.args)
    else:
        response = requests.post(url, headers=headers, json=request.json)
    return jsonify(response.json()), response.status_code

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)