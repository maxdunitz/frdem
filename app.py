from flask import Flask, request, flash, url_for, send_file, session, redirect, Response, render_template_string, jsonify
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
import io
from zoneinfo import ZoneInfo
from twilio.http.http_client import TwilioHttpClient
from twilio.base.exceptions import TwilioRestException
from flask_sqlalchemy import SQLAlchemy
import nexmo 

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
http_client = TwilioHttpClient(timeout=20)
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

recipients = [RECIPIENT1, RECIPIENT2, RECIPIENT3, RECIPIENT4]
def whomst_to_call(req, lang): # FRDEM
    if req == '1': # voting, english or french
        return random.choice(recipients)
    elif req == '2': # general, english or french
        return random.choice(recipients)
    elif req == '3': # press inquiries, english or french
        return RECIPIENT_MEDIA

def choose_recipient(): # VFA
    numbers = [r[1:] for r in recipients]
    return random.choice(numbers)

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


# Configure Postgres
uri = os.getenv("DATABASE_URL")
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_recycle": 280,  # Recycle connections every 4-5 minutes
    "pool_pre_ping": True, # Check if the connection is alive before using it
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db_pg = SQLAlchemy(app)

with app.app_context():
    db_pg.create_all()

class CommunicationLog(db_pg.Model):
    id = db_pg.Column(db_pg.Integer, primary_key=True)
    provider = db_pg.Column(db_pg.String(20))     # 'Twilio' or 'Nexmo'
    comm_type = db_pg.Column(db_pg.String(20))    # 'SMS' or 'Call'
    direction = db_pg.Column(db_pg.String(20))    # 'Inbound' or 'Outbound'
    from_num = db_pg.Column(db_pg.String(50))
    to_num = db_pg.Column(db_pg.String(50))
    content = db_pg.Column(db_pg.Text)            # SMS text or Status
    recording_url = db_pg.Column(db_pg.Text)
    sid = db_pg.Column(db_pg.String(100))         # Store Twilio CallSid or Nexmo UUID here
    timestamp = db_pg.Column(db_pg.DateTime, default=france_now)

# Initialize database
with app.app_context():
    db_pg.create_all()

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


###################### TWILIO ROUTES #########################

@app.route("/receive_sms", methods=['GET', 'POST'])
@csrf.exempt
def receive_sms():
    msg = request.form['Body']
    number = request.form['From']
    to = request.form['To']
    
    # SAVE TO POSTGRES
    new_log = CommunicationLog(
        provider='Twilio',
        comm_type='SMS',
        direction='Inbound',
        from_num=number,
        to_num=to,
        content=msg
    )
    db_pg.session.add(new_log)
    db_pg.session.commit()

    # Continue with email...
    now = france_now()
    subject = f"Incoming SMS from {number}"
    html = f"<p>From: {number}</p><p>Body: {msg}</p>"
    send_email(FROM_EMAIL, RECIPIENT_EMAILS, subject, html)
    return str(MessagingResponse())

## RECEIVE CALL ##
@app.route("/receive_call", methods=['GET', 'POST'])
@csrf.exempt
def receive_call():
    call_sid = request.form.get('CallSid')
    from_num = request.form.get('From', 'Unknown')
    to_num = request.form.get('To', CALLER_ID) # Your Twilio Number

    new_log = CommunicationLog(
        provider='Twilio',
        comm_type='Call',
        direction='Inbound',
        from_num=from_num,
        to_num=to_num,
        sid=call_sid,             # Store the link!
        content='Call Started - In Menu'
    )
    db_pg.session.add(new_log)
    db_pg.session.commit()
    
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

    new_log = CommunicationLog(
        provider='Twilio',
        comm_type='Call',
        direction='Inbound',
        from_num=from_number,
        content=f"Voicemail: {transcription_text}",
        recording_url=url_recording
    )
    db_pg.session.add(new_log)
    db_pg.session.commit()
    
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
    Return the original caller's number.
    """
    def _extract_from(obj):
        if not obj:
            return None
        v = getattr(obj, 'from_', None)
        if v:
            return v
        props = getattr(obj, '_properties', {}) or {}
        return props.get('from')

    # parent leg holds the real caller
    parent_sid = getattr(call, 'parent_call_sid', None) \
                 or getattr(call, '_properties', {}).get('parent_call_sid')
    base_sid = parent_sid or call.sid

    try:
        base = twilio_client.calls(base_sid).fetch()
        print("BASE", base)
    except TwilioRestException:
        base = call  # fail open

    num = _extract_from(base)
    print("NUM", num)
    if num:
        return num

    props = getattr(base, '_properties', {}) or {}
    for k in ('from_formatted', 'forwarded_from', 'caller_name'):
        if props.get(k):
            return props[k]
    return 'Unknown'

@app.route("/admin/calls")
@requires_auth
def admin_calls():
    items = []
    error_msg = None

    try:
        calls = twilio_client.calls.list(limit=30)

        for c in calls:
            #if c.direction != 'inbound':
            #    continue

            from_number = c.from_formatted if hasattr(c, 'from_formatted') and c.from_formatted else c.from_
            
            recording_url = None
            transcription_text = None
            transcription_url = None

            try:
                recs = twilio_client.recordings.list(call_sid=c.sid, limit=1)
                if recs:
                    rec = recs[0]
                    recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCT}/Recordings/{rec.sid}.mp3"

                    # 2. Look for Transcriptions
                    try:
                        trans = twilio_client.transcriptions.list(recording_sid=rec.sid, limit=1)
                        if trans:
                            t = trans[0]
                            transcription_text = t.transcription_text
                            transcription_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCT}/Transcriptions/{t.sid}.json"
                    except Exception:
                        pass
            except Exception:
                pass

            items.append({
                "sid": c.sid,
                "start_time": c.start_time,
                "from_": from_number,
                "to": c.to,
                "status": c.status,
                "duration": c.duration,
                "recording_url": recording_url,
                "transcription_text": transcription_text,
                "transcription_url": transcription_url,
            })

    except TwilioRestException as e:
        error_msg = f"Twilio error: {e.msg}"
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"

    banner = ""
    if error_msg:
        banner = f'<div style="padding:.75rem;background:#fff3cd;color:#664d03;border:1px solid #ffecb5;border-radius:6px;margin-bottom:1rem;"><strong>Note:</strong> {error_msg}</div>'

    html = TEMPLATE.replace("<body>", f"<body>{banner}", 1)
    return render_template_string(html, items=items)

### NEXMO PHONE LINE ###
# load environment variables
WELCOME = os.getenv("WELCOME_OHGODVOTE")
FRENCH = os.getenv("FRENCH_OHGODVOTE")
ENGLISH = os.getenv("ENGLISH_OHGODVOTE")
NEXMO_NUMBER = os.getenv("NEXMO_NUMBER")

NEXMO_PRIVATE_KEY = os.getenv("NEXMO_PRIVATE_KEY")
NEXMO_APPLICATION_ID = os.getenv("NEXMO_APPLICATION_ID")
NEXMO_API_KEY = os.getenv("NEXMO_API_KEY")
NEXMO_API_SECRET = os.getenv("NEXMO_API_SECRET")

# Initialize the Nexmo client using the string directly
# Note: The library is smart enough to handle the string if it looks like a key
client = nexmo.Client(
    application_id=NEXMO_APPLICATION_ID,
    private_key=io.StringIO(NEXMO_PRIVATE_KEY),
)

@app.route("/admin/history")
@requires_auth
def admin_history():
    logs = CommunicationLog.query.order_by(CommunicationLog.timestamp.desc()).limit(100).all()
    
    return render_template_string("""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Communication History</title>
    <style>
        body { font-family: system-ui, sans-serif; background: #f0f2f5; padding: 20px; }
        .log-container { max-width: 800px; margin: 0 auto; }
        .card { 
            background: white; border-radius: 10px; padding: 15px; margin-bottom: 12px;
            border-left: 8px solid #ccc; box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            display: flex; justify-content: space-between; align-items: center;
        }
        .twilio { border-left-color: #F22F46; }
        .nexmo { border-left-color: #0077ff; }
        .from-num { font-size: 1.1rem; font-weight: bold; color: #1a202c; }
        .meta { color: #666; font-size: 0.85rem; margin-top: 4px; }
        .content { margin-top: 8px; font-style: italic; }
        audio { height: 35px; margin-top: 10px; display: block; }
        .time { text-align: right; color: #999; font-size: 0.8rem; }
    </style>
</head>
<body>
    <div class="log-container">
        <h1>Unified VFA/FRDEM History</h1>
{% for log in logs %}
<div class="card {{ log.provider|lower }}">
    <div style="flex-grow: 1;">
        <div class="from-num">From: {{ log.from_num }}</div>
        <div class="meta">
            <strong>{{ log.provider }}</strong> | {{ log.comm_type }} | 
            <span style="color: #2c5282;">To: {{ log.to_num }}</span>
        </div>
        <div class="content">{{ log.content }}</div>
        
        <div style="font-size: 0.7rem; color: #aaa; margin-top: 5px;">ID: {{ log.sid }}</div>
        
        {% if log.recording_url %}
            {% endif %}
    </div>
    <div class="time">
        {{ log.timestamp.strftime('%Y-%m-%d') }}<br>
        {{ log.timestamp.strftime('%H:%M:%S') }}
    </div>
</div>
{% endfor %}
    </div>
</body>
</html>
    """, logs=logs)


@app.route("/answer", methods=["GET", "POST"])
@csrf.exempt # Nexmo webhooks need CSRF exempt
def nexmo_answer():
    """Initial IVR Greeting"""
    receive_numbers = request.url_root + "language"
    return jsonify([
        {"action": "stream", "streamUrl": [WELCOME], "bargeIn": True},
        {"action": "input", "maxDigits": 1, "eventUrl": [receive_numbers]}
    ])

@app.route("/language", methods=["POST"])
@csrf.exempt
def nexmo_pick_language():
    data = request.get_json()
    digits = data.get('dtmf', '1')
    them = data.get('from', 'unknown')
    conv_id = data.get('conversation_uuid')

    if digits == "2":
        route_url = request.url_root + "voicemail_french"
        language = 'french'
    else:
        route_url = request.url_root + "voicemail_english"
        language = 'english'

    recipient = choose_recipient()

    new_call = CommunicationLog(
        provider='Nexmo',
        comm_type='Call',
        direction='Inbound',
        from_num=them,
        to_num=NEXMO_NUMBER,
        sid=conv_id,      
        content=f"VFA call routing in {language}. Sent to {recipient}."
    )
    db_pg.session.add(new_call)
    db_pg.session.commit()
    
    try:
        client.send_message({
            "from": NEXMO_NUMBER.lstrip('+'),
            "to": recipient.lstrip('+'),
            "text": f"VFA voter-help call from {them}. Language: {language}"
        })
    except: 
        print("text failed", NEXMO_NUMBER, recipient)

    return jsonify([
        {
            "action": "connect",
            "timeout": "20",  # Give the volunteer 20 seconds to pick up
            "from": NEXMO_NUMBER,
            "endpoint": [{"type": "phone", "number": recipient}]
        },
        {
            # This ONLY runs if the 'connect' above fails or times out
            "action": "talk",
            "text": "Please wait while we transfer you to voicemail." if language == 'english' else "Veuillez patienter, nous vous transférons vers la messagerie.",
            "language": "en-US" if language == 'english' else "fr-FR"
        },
        {
            # This redirects the call flow to the actual recording logic
            "action": "talk", 
            "text": "Redirecting", 
            "voiceName": "Kimberly"
        }
    ])
    
@app.route("/play_nexmo")
@requires_auth
def play_nexmo():
    recording_url = request.args.get('url')
    download = request.args.get('download', 'false').lower() == 'true'
    
    if not recording_url:
        return "No URL provided", 400

    # Fetch the file from Nexmo using your API credentials
    response = requests.get(recording_url, auth=(NEXMO_API_KEY, NEXMO_API_SECRET), stream=True)
    
    if response.status_code == 200:
        headers = {"Content-Type": "audio/mpeg"}
        if download:
            # This forces the browser to download the file with a clean name
            filename = f"voicemail_{int(time.time())}.mp3"
            headers["Content-Disposition"] = f"attachment; filename={filename}"
            
        return Response(response.content, headers=headers)
    else:
        return f"Failed to fetch recording: {response.status_code}", 500

@app.route("/new-recording", methods=["POST"])
@csrf.exempt
def nexmo_new_recording():
    data = request.json
    conv_id = data.get('conversation_uuid')
    recording_url = data.get('recording_url')
    original_call = CommunicationLog.query.filter_by(sid=conv_id).first()
    caller_id = original_call.from_num if original_call else "Unknown Caller"
    if not recording_url:
        return "", 204

    new_log = CommunicationLog(
        provider='Nexmo',
        comm_type='Call',
        direction='Inbound',
        from_num=caller_id,
        content='New Voicemail',
        recording_url=recording_url # We keep the raw URL here for the proxy to use
    )
    db_pg.session.add(new_log)
    db_pg.session.commit()

    proxy_link = f"{request.url_root}play_nexmo?url={recording_url}"
    
    subject = f"New VFA Nexmo Voicemail from {caller_id}"
    html = f"<p>Voicemail from: {caller_id}</p><p><a href='{proxy_link}'>CLICK HERE TO LISTEN TO RECORDING</a></p>"
    send_email(FROM_EMAIL, RECIPIENT_EMAILS, subject, html)
    
    return "", 204


@app.route("/voicemail_english", methods=["POST"])
@csrf.exempt
def voicemail_english(): 
    print("Nexmo: Entering English Voicemail")
    newrecording = request.url_root + "new-recording"
    
    return jsonify([
        {
            "action": "stream",
            "streamUrl": [ENGLISH]
        },
        {
            "action": "record",
            "beepStart": True,
            "eventUrl": [newrecording],
            "endOnSilence": 3,
            "format": "mp3"
        },
        {
            "action": "talk",
            "text": "Thank you for your message. Goodbye.",
            "style": 0 # Standard voice
        }
    ])

@app.route("/voicemail_french", methods=["POST"])
@csrf.exempt
def voicemail_french():
    print("Nexmo: Entering French Voicemail")
    newrecording = request.url_root + "new-recording"
    
    return jsonify([
        {
            "action": "stream",
            "streamUrl": [FRENCH]
        },
        {
            "action": "record",
            "beepStart": True,
            "eventUrl": [newrecording],
            "endOnSilence": 3,
            "format": "mp3"
        },
        {
            "action": "talk",
            "text": "Merci pour votre message. Au revoir.",
            "language": "fr-FR",
            "style": 0
        }
    ])
@app.route("/inbound-sms", methods=["POST"])
@csrf.exempt
def nexmo_inbound_sms():
    data = request.get_json()
    msg = data.get('text', 'No text')
    them = data.get('msisdn', 'Unknown')

    # SAVE TO SHARED POSTGRES
    new_log = CommunicationLog(
        provider='Nexmo',
        comm_type='SMS',
        direction='Inbound',
        from_num=them,
        content=msg
    )
    db_pg.session.add(new_log)
    db_pg.session.commit()

    send_email(FROM_EMAIL, RECIPIENT_EMAILS, f"Nexmo SMS from {them}", f"Body: {msg}")
    return "", 204
