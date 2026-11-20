import httpx
import os
import urllib.parse
from harvey.config import load_env

env = load_env()
account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
from_number = os.getenv("TWILIO_PHONE_NUMBER", "")
to_number = os.getenv("TWILIO_TEST_TO_NUMBER", "")

twiml_content = """<Response>
    <Say language="id-ID" voice="Polly.Ardi">
        Halo Pak, selamat siang! Perkenalkan saya Ahnaf, asisten AI dari PT Andis Sentral Laboratorium. 
        Senang sekali bisa terhubung langsung dengan nomor telepon Bapak. 
        Sistem telepon suara otonom Harvey saat ini sudah aktif seratus persen dan siap beroperasi untuk penawaran alat laboratorium di seluruh Indonesia. 
        Terima kasih banyak atas waktunya Pak, salam sukses untuk PT Andis Sentral Laboratorium!
    </Say>
</Response>"""

encoded_twiml = urllib.parse.quote(twiml_content)
twimlet_url = f"https://twimlets.com/echo?Twiml={encoded_twiml}"

api_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
data = {
    "To": to_number,
    "From": from_number,
    "Url": twimlet_url
}

print(f"Memulai panggilan suara kustom Bahasa Indonesia ke {to_number}...")
with httpx.Client(timeout=30.0) as client:
    r = client.post(api_url, data=data, auth=(account_sid, auth_token))
    print(f"HTTP Status: {r.status_code}")
    res = r.json()
    print("Call SID:", res.get("sid"))
    print("Call Status:", res.get("status"))
