from flask import Flask, request, flash, url_for, send_file, session, redirect, Response, render_template_string
from twilio import twiml
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Dial, Say, Play, Gather
from twilio.rest import Client
import os, sys, json, datetime, re, requests, redis, psycopg2, gspread, time
from flask_wtf import CSRFProtect
from functools import wraps
from sqlalchemy import desc
from dotenv import load_dotenv, find_dotenv
from authlib.integrations.flask_client import OAuth
import random
import resend
from zoneinfo import ZoneInfo
from twilio.http.http_client import TwilioHttpClient
from twilio.base.exceptions import TwilioRestException

## load environment variables
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS")  # set in Koyeb secrets

TWILIO_ACCT = os.environ["TWILIO_ACCT"]
TWILIO_SECRET = os.environ["TWILIO_SECRET"]

RECIPIENT1 = os.environ["RECIPIENT1"] # gets the general and voter help inquiries
RECIPIENT2 = os.environ["RECIPIENT2"] # gets the general and voter help inquiries
RECIPIENT3 = os.environ["RECIPIENT3"] # gets the general and voter help inquiries
RECIPIENT4 = os.environ["RECIPIENT4"] # gets the general and voter help inquiries
RECIPIENT_MEDIA = os.environ["RECIPIENT_MEDIA"] # gets media inquiries
RECIPIENT_DEBUGGING = os.environ["RECIPIENT_DEBUGGING"] # gets debugging texts

CALLER_ID = os.environ["CALLER_ID"]
CALLER_ID_US = os.environ["CALLER_ID_US"]

FROM_EMAIL = os.environ['FROM_EMAIL']
RECIPIENT_EMAILS = [os.environ['RESPONSE_LIST'], os.environ['TECH_LIST']]

SECRET_KEY = os.environ['SECRET_KEY']
CALLBACK_URL = os.environ['CALLBACK_URL']

TEXTBOT_NAME = "DAF TEXT BOT"
CALLBOT_NAME = "DAF CALL BOT"

# Setup Twilio client
http_client = TwilioHttpClient(timeout=10)
twilio_client = Client(TWILIO_ACCT, TWILIO_SECRET, http_client=http_client)

## VOICE MESSAGE LOCATIONS ##
ENGLISH_URL = os.environ['ENGLISH_URL']
VOICEMAIL_FRENCH_URL = os.environ['VOICEMAIL_FRENCH_URL']
VOICEMAIL_ENGLISH_URL = os.environ['VOICEMAIL_ENGLISH_URL']
INTRO_URL = os.environ['INTRO_URL']
FRENCH_URL = os.environ['FRENCH_URL']
FDR_URL = os.environ['FDR_URL']

## TIME ##
def france_now():
    return datetime.datetime.now(ZoneInfo("Europe/Paris")) 

## CALL HANDLING ##
def is_business_hours():
    return france_now().hour >= 10 and france_now().hour <= 21

def whomst_to_call(req, lang):
    recipients = [RECIPIENT1, RECIPIENT2, RECIPIENT3, RECIPIENT4]
    if req == '1': # voting, english or french
        return random.choice(recipients)
    elif req == '2': # general, english or french
        return random.choice(recipients)
    elif req == '3': # press inquiries, english or french
        return RECIPIENT_MEDIA

def get_help_type(choice):
    if choice == '1':
        return "voter inquiry"
    elif choice == '2':
        return 'general inquiry'
    elif choice == '3':
        return 'media inquiry'

## CLEAN NUMBER ##
def clean_number(s):
    sdigitsonly = re.sub('[^0-9]+', '', s)
    return "+"+sdigitsonly

def correct_number(s):
    if len(s) == 11 and s[1] == '0': # likely misformatted French (no country code starts with 0, no US area code starts with 0)
        return ("+33"+s[2:], CALLER_ID)
    elif len(s) == 11 and int(s[1]) >= 2: # likely misformatted US (beginning with area code)
        return ("+1"+s[1:], CALLER_ID_US)
    elif len(s) == 12 and s[0:3] == '+33': # definite france
        return (s, CALLER_ID)
    elif len(s) == 12 and s[0:4] == '+330': # definite france
        return ('+33'+s[4:], CALLER_ID)
    elif len(s) == 12 and s[0:2] == '+1': # likely us
        return (s, CALLER_ID_US)
    elif len(s) >= 12 and len(s) <= 15:
        return (s, CALLER_ID)
    else:
        return ('invalid', CALLER_ID)


###################### SET UP FLASK APP #########################


## CONFIGURE APP ##

app = Flask(__name__)
csrf = CSRFProtect(app)
app.secret_key = SECRET_KEY

###################### EMAIL OUR ACCOUNT ######################

def send_email(f, t, subject, html):
    try: 
        resend.api_key = os.environ.get('RESEND_API_KEY')
        r = resend.Emails.send({
            "from": f,
             "to": t, 
             "subject": subject,
             "html": html
        })
    except Exception as e:

        twilio_client.messages.create(
            body=f"[DEBUG] {subject}",
            from_=CALLER_ID,          
            to=RECIPIENT_DEBUGGING
        )

        print(e, type(e))
        print(e.args)


###################### ROUTES #########################


## RECEIVE SMS ##
@app.route("/receive_sms", methods=['GET', 'POST'])
@csrf.exempt
def receive_sms():
    ## GET INCOMING INFO ##
    msg = request.form['Body'] # THE TEXT ITSELF
    number = request.form['From'] # THE SENDER'S NUMBER
    to = request.form['To'] # THE INCOMING NUMBER
    now = france_now()
    subject = f"Incoming SMS from {number} @ {now.isoformat()}"
    html = f"<p>To: {to}</p><p>From: {number}</p><p>Body: {msg}</p>"
    send_email(FROM_EMAIL, RECIPIENT_EMAILS, subject, html)
    return str(MessagingResponse())

## RECEIVE CALL ##
@app.route("/receive_call", methods=['GET', 'POST'])
@csrf.exempt
def receive_call():
    print("RECEIVE_CALL")
    # Start our TwiML response
    resp = VoiceResponse()

    # Read a message aloud to the caller
    g = Gather(num_digits=1, action='/intro') # looking for one digit
    g.play(INTRO_URL, loop=1)
    resp.append(g)
    resp.redirect('/intro')
    return str(resp)

@app.route("/intro", methods=['GET', 'POST'])
@csrf.exempt
def receive_language_digits():
    print("GOT HERE  - language digits")
    resp = VoiceResponse()
    if 'Digits' in request.values.to_dict(flat=False):
        choice = request.values.to_dict(flat=False)['Digits'][0]
        if choice == "2":
            session['language'] = 'french'
            g = Gather(num_digits=1, action='/route')
            g.play(FRENCH_URL, loop=3)
            resp.append(g)
            resp.redirect('/route')
            return str(resp)
    session['language'] = 'english'
    g = Gather(num_digits=1, action='/route')
    g.play(ENGLISH_URL, loop=3)
    resp.append(g)
    resp.redirect('/route')
    return str(resp)

@app.route("/route", methods=["GET", "POST"])
@csrf.exempt
def french_route():
    resp = VoiceResponse()
    if not is_business_hours():
        resp.redirect('/end_call')
        return str(resp)
    choice = request.values.get('Digits')

    if choice in ['1', '2', '3']:
        language = session.get('language', 'english')
        to_call = whomst_to_call(choice, language)
        help_type = get_help_type(choice)
        incoming_caller_id = request.values.get('From')
        message_body = f"Caller {incoming_caller_id} with {help_type} (in {language})."
        message = twilio_client.messages.create(body=message_body, from_=CALLER_ID, to=to_call)
        resp.dial(to_call, timeout=12, action="/end_call")
        return str(resp)
    resp.say("I'm sorry, I didn't quite get that.")
    resp.redirect("/route")
    return str(resp)

@app.route("/end_call", methods=["GET", "POST"])
@csrf.exempt
def end_call_french():
    resp = VoiceResponse()
    language = session.get('language', 'english')
    if language == 'english':
        resp.play(VOICEMAIL_ENGLISH_URL)
    elif language == 'french':
        resp.play(VOICEMAIL_FRENCH_URL)
    resp.record(max_length="60", transcribe=True, action="/postscript", transcribe_callback="/send_transcription")
    resp.redirect("/postscript")
    return str(resp)


@app.route('/postscript', methods=['GET', 'POST'])
@csrf.exempt
def end_call():
    print("END CALL")
    """Thanks a caller for their recording and hangs up"""
    resp = VoiceResponse()
    resp.say("Thanks for your message.")
    resp.play(FDR_URL)
    resp.hangup()
    return str(resp)

@app.route("/send_transcription", methods=["POST"])
@csrf.exempt
def send_transcription():
    """ Creates a client object and returns the transcription text to an SMS message"""
    
    transcription_text = request.form.get('TranscriptionText')
    url_recording = request.form.get('RecordingUrl')
    from_number = request.form.get('From')

    if not transcription_text:
        print("Transcription not ready or failed.")
        return "OK", 200

    message_body = f'''<h1>NEW VOICEMAIL TO DA FRANCE</h1>
                       <br>
                       <p>MACHINE TRANSCRIPTION: {transcription_text}</p>
                       <br>
                       <a href="{url_recording}">LISTEN TO RECORDING</a>'''

    try:
        send_email(FROM_EMAIL, RECIPIENT_EMAILS, f'NEW DAF VOICEMAIL FROM {from_number}', message_body)
    except Exception as e:
        print(e, type(e))
        print(e.args)
    return str(message_body)




#### DISPLAY LAST 10 CALLS IN PASSWORD-PROTECTED DEBUGGING SITE ####

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def authenticate():
    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="Restricted"'}
    )

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return wrapper


TEMPLATE = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Last 10 Calls + Voicemail</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem; }
h1 { margin-bottom: 0.5rem; }
small { color: #666; }
.card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
label { font-weight: 600; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
audio { width: 100%; margin-top: 0.5rem; }
</style></head>
<body>
  <h1>Last 10 Calls</h1>
  <small>Data fetched live from Twilio REST API</small>

  {% for row in items %}
    <div class="card">
      <div class="grid">
        <div>
          <div><label>When:</label> {{ row.start_time or "—" }}</div>
          <div><label>From:</label> {{ row.from_ }}</div>
          <div><label>To:</label> {{ row.to }}</div>
          <div><label>Status:</label> {{ row.status }}</div>
          <div><label>Duration (s):</label> {{ row.duration or "—" }}</div>
          <div><label>Call SID:</label> <span class="mono">{{ row.sid }}</span></div>
        </div>
        <div>
          <div><label>Voicemail / Recording:</label>
            {% if row.recording_url %}
              <div><audio controls src="{{ row.recording_url }}"></audio></div>
              <div><a href="{{ row.recording_url }}" target="_blank" rel="noopener">Open recording</a></div>
            {% else %}
              <div>— No recording found</div>
            {% endif %}
          </div>
          <div style="margin-top:0.75rem;"><label>Transcription:</label>
            <div>{{ row.transcription_text or "—" }}</div>
            {% if row.transcription_url %}
              <div><a href="{{ row.transcription_url }}" target="_blank" rel="noopener">Open transcription</a></div>
            {% endif %}
          </div>
        </div>
      </div>
    </div>
  {% endfor %}
</body></html>
"""

def get_from_number(call) -> str:
    """
    Robustly extract the caller's number from a CallInstance across helper versions. Fallback: 'Unknown'
    """
    v = getattr(call, 'from_', None)
    if v: 
        return v

    props = getattr(call, '_properties', {}) or {}
    for key in ('from', 'from_formatted', 'forwarded_from', 'caller_name'):
        if props.get(key):
            return props[key]

    return 'Unknown'


@app.route("/admin/calls")
@requires_auth
def admin_calls():
    items = []
    error_msg = None

    try:
        calls = twilio_client.calls.list(limit=10, page_size=10)

        for c in calls:
            from_number = get_from_number(c)           
            to_number   = getattr(c, 'to', None) or getattr(c, '_properties', {}).get('to')

            from_number = _safe_call_field(c, 'from_')   # falls back to JSON 'from'
            to_number   = _safe_call_field(c, 'to')

            recording_url = None
            transcription_text = None
            transcription_url  = None

            recs = twilio_client.recordings.list(call_sid=c.sid, limit=1)
            if recs:
                rec = recs[0]
                recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCT}/Recordings/{rec.sid}.mp3"

                trans = twilio_client.recordings(rec.sid).transcriptions.list(limit=1)
                if trans:
                    t = trans[0]
                    transcription_text = t.transcription_text
                    transcription_url  = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCT}/Transcriptions/{t.sid}.json"

            items.append({
                "sid": c.sid,
                "start_time": c.start_time,
                "from_": from_number,
                "to": to_number,
                "status": c.status,
                "duration": c.duration,
                "recording_url": recording_url,
                "transcription_text": transcription_text,
                "transcription_url": transcription_url,
            })

    except TwilioRestException as e:
        error_msg = f"Twilio error {e.status} / {getattr(e, 'code', '?')}: {e.msg or 'unknown error'}"
    except Exception as e:
        error_msg = f"Unexpected error: {type(e).__name__}: {str(e)}"

    # Always render; include a banner if any error occurred for visibility.
    banner = ""
    if error_msg:
        banner = f'<div style="padding:.75rem;background:#fff3cd;color:#664d03;border:1px solid #ffecb5;border-radius:6px;margin-bottom:1rem;"><strong>Note:</strong> {error_msg}</div>'

    html = TEMPLATE.replace("<body>", f"<body>{banner}", 1)
    return render_template_string(html, items=items)
