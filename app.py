from flask import Flask, request, flash, url_for, send_file, session, redirect, Response, render_template_string, jsonify
from twilio import twiml
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Dial, Say, Play, Gather
from twilio.rest import Client
import os, sys, json, datetime, re, requests, redis, psycopg2, gspread, time
import hmac
import threading
from urllib.parse import quote
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
ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "").strip()  # set in Koyeb secrets

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

def format_paris_time(utc_dt):
    if not utc_dt:
        return ""
    # 1. Ensure the datetime knows it is UTC
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=ZoneInfo("UTC"))
    
    # 2. Convert to Europe/Paris (handles DST automatically!)
    paris_tz = ZoneInfo("Europe/Paris")
    return utc_dt.astimezone(paris_tz)

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
    # lstrip('+'), not r[1:]: the latter silently eats a real digit from any
    # RECIPIENT env var that is not written with a leading +, producing a
    # number Vonage cannot dial.
    numbers = [r.lstrip('+') for r in recipients]
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

app.jinja_env.filters['paris_time'] = format_paris_time

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

# Initialize database. On Koyeb's free Postgres this wakes the compute and
# spends ~5 minutes of the 5 hour/month budget, so it runs once, not twice --
# and a database outage must not stop the phone lines from booting.
with app.app_context():
    try:
        db_pg.create_all()
    except Exception as e:
        print("create_all failed at boot; continuing without it:", e)


def log_comm(**fields):
    """Best-effort write to CommunicationLog.

    Logging is bookkeeping: if Postgres is asleep, out of free-tier hours or
    unreachable, we must still return valid TwiML/NCCO or the carrier drops
    the caller. Never let this raise.
    """
    try:
        db_pg.session.add(CommunicationLog(**fields))
        db_pg.session.commit()
        return True
    except Exception as e:
        print("CommunicationLog write FAILED:", type(e).__name__, e)
        try:
            db_pg.session.rollback()
        except Exception:
            pass
        return False


def find_comm(**filters):
    """Best-effort read. Returns None instead of raising."""
    try:
        return CommunicationLog.query.filter_by(**filters).first()
    except Exception as e:
        print("CommunicationLog read FAILED:", type(e).__name__, e)
        try:
            db_pg.session.rollback()
        except Exception:
            pass
        return None


def run_in_background(fn, *args, **kwargs):
    """Run fn off the request path, inside this same process.

    This is a plain thread, not a worker queue: no Redis, no second service.
    Carriers give us a hard budget to answer a webhook (Vonage ~5s, Twilio
    ~15s), and a sleeping database or a slow SMS API can eat all of it. Work
    that the caller does not need to wait for goes here so the response ships
    immediately.

    The thread gets its own app context and always releases its database
    session -- a leaked connection would keep Postgres from suspending and
    quietly burn the free tier's monthly compute budget.

    Pass values, never the request object; `request` does not exist here.
    """
    def runner():
        with app.app_context():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                print("background task", getattr(fn, "__name__", fn),
                      "FAILED:", type(e).__name__, e)
            finally:
                try:
                    db_pg.session.remove()
                except Exception:
                    pass

    threading.Thread(target=runner, daemon=True).start()

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


def alert_transfer_failure(provider, summary, details):
    """Email the tech list when a transfer fails for a reason that is not
    simply the volunteer being unavailable.

    Deliberately narrow: no-answer, busy and timeout are ordinary outcomes
    and are not reported. rejected/failed mean the carrier or our config
    refused the leg, which nobody would otherwise notice -- the caller just
    quietly lands in voicemail.
    """
    send_email(
        FROM_EMAIL,
        RECIPIENT_EMAILS,
        f"[DAF] {provider} transfer failed: {summary}",
        f"<h3>{provider} could not transfer a caller</h3>"
        f"<p>{summary}</p><pre>{details}</pre>",
    )


@app.route("/health")
@csrf.exempt
def health():
    """Cheap keep-alive target. Touches no DB, no Twilio, no Nexmo."""
    return "ok", 200


###################### TWILIO ROUTES #########################

@app.route("/receive_sms", methods=['GET', 'POST'])
@csrf.exempt
def receive_sms():
    msg = request.form['Body']
    number = request.form['From']
    to = request.form['To']
    
    # SAVE TO POSTGRES
    log_comm(
        provider='Twilio',
        comm_type='SMS',
        direction='Inbound',
        from_num=number,
        to_num=to,
        content=msg
    )

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

    # This is the first webhook of the call, so a sleeping Postgres would
    # delay the menu itself. Log in the background and play the greeting now.
    run_in_background(
        log_comm,
        provider='Twilio',
        comm_type='Call',
        direction='Inbound',
        from_num=from_num,
        to_num=to_num,
        sid=call_sid,             # Store the link!
        content='Call Started - In Menu',
    )

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
        # Same reasoning as /language: notifying the volunteer is a network
        # round trip the caller should not wait through before we dial.
        run_in_background(
            twilio_client.messages.create,
            body=message_body, from_=CALLER_ID, to=to_call,
        )
        resp.dial(to_call, timeout=12, action="/end_call")
        return str(resp)
    resp.say("I'm sorry, I didn't quite get that.")
    resp.redirect("/route")
    return str(resp)

@app.route("/end_call", methods=["GET", "POST"])
@csrf.exempt
def end_call_french():
    resp = VoiceResponse()

    # <Dial> fires this action on every outcome, including a conversation
    # that went fine. Without this check, a caller whose volunteer hangs up
    # first gets dropped into "leave a message after the tone".
    dial_status = request.values.get('DialCallStatus')
    if dial_status == 'completed':
        resp.play(FDR_URL)
        resp.hangup()
        return str(resp)

    if dial_status == 'failed':
        run_in_background(
            alert_transfer_failure,
            provider="Twilio",
            summary=f"dial failed for CallSid {request.values.get('CallSid')}",
            details=json.dumps(request.values.to_dict(), indent=2, default=str),
        )

    language = session.get('language', 'english')
    if language == 'english':
        resp.play(VOICEMAIL_ENGLISH_URL)
    elif language == 'french':
        resp.play(VOICEMAIL_FRENCH_URL)
    resp.record(max_length="60", transcribe=True, action="/postscript", recording_status_callback="/handle_recording_status", transcribe_callback="/send_transcription")
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

@app.route("/handle_recording_status", methods=["POST"])
@csrf.exempt
def handle_recording_status():
    url_recording = request.form.get('RecordingUrl')
    call_sid = request.form.get('CallSid')
    status = request.form.get('RecordingStatus')

    # Twilio's recordingStatusCallback does NOT include From/To -- only the
    # recording fields plus CallSid. Resolve the caller from the call itself.
    from_number = request.form.get('From')
    if not from_number and call_sid:
        try:
            from_number = get_from_number(twilio_client.calls(call_sid).fetch())
        except Exception as e:
            print("could not resolve caller for", call_sid, e)
    from_number = from_number or 'Unknown'

    if status == 'completed' and url_recording:

        subject = f"NEW VOICEMAIL FROM {from_number} TO DA FRANCE"
        html = f"""
            <h3>New Voicemail Received</h3>
            <p><strong>From:</strong> {from_number}</p>
            <p><a href="{url_recording}">Listen to Recording</a></p>
        """
        send_email(FROM_EMAIL, RECIPIENT_EMAILS, subject, html)

    return "OK", 200



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

    message_body = f'''<h1>NEW VOICEMAIL TRANSCRIPTION FROM {from_number} TO DA FRANCE</h1>
                       <br>
                       <p>MACHINE TRANSCRIPTION: {transcription_text}</p>
                       <br>
                       <a href="{url_recording}">LISTEN TO RECORDING</a>'''

    try:
        send_email(FROM_EMAIL, RECIPIENT_EMAILS, f'NEW DAF VOICEMAIL FROM {from_number}', message_body)
    except Exception as e:
        print(e, type(e))
        print(e.args)

    log_comm(
        provider='Twilio',
        comm_type='Call',
        direction='Inbound',
        from_num=from_number,
        content=f"Voicemail: {transcription_text}",
        recording_url=url_recording
    )
    
    return str(message_body)


#### DISPLAY LAST 10 CALLS IN PASSWORD-PROTECTED DEBUGGING SITE ####

def check_auth(username, password):
    if not ADMIN_PASS:
        return False
    return (hmac.compare_digest(username or "", ADMIN_USER)
            and hmac.compare_digest(password or "", ADMIN_PASS))

def authenticate():
    # Without this, a missing ADMIN_PASS is indistinguishable from a wrong
    # password: the browser just re-prompts forever.
    if not ADMIN_PASS:
        return Response(
            "ADMIN_PASS is not set on this deployment, so no password can "
            "ever match. Set it in the Koyeb service environment variables.",
            500,
        )
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

# ARCEP bars A2P outbound calls to France whose CLI is a French mobile
# (336/337). NEXMO_NUMBER is a 336 number, so it cannot be the caller ID on a
# transfer -- local carriers reject the leg with SIP 403 / detail "restricted".
# Set NEXMO_OUTBOUND_CLI to a Vonage number outside those ranges (a 01-05
# geographic or an 09 number). Inbound calls and SMS keep using NEXMO_NUMBER,
# so the advertised number does not change.
NEXMO_OUTBOUND_CLI = (os.getenv("NEXMO_OUTBOUND_CLI") or NEXMO_NUMBER or "").lstrip("+")

# Experiment flag. Vonage's NCCO reference says "from" must be one of your
# Vonage virtual numbers "as the call won't connect otherwise", which would
# make caller-ID passthrough impossible. Other sources claim it works when
# caller and destination are in the same country, which is our case. Set
# NEXMO_USE_CALLER_CLI=1 to try presenting the inbound caller's own number on
# the transfer leg; the CONNECT EVENT log line settles it in one call. Unset
# it to fall straight back to NEXMO_OUTBOUND_CLI.
NEXMO_USE_CALLER_CLI = (os.getenv("NEXMO_USE_CALLER_CLI") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

if NEXMO_OUTBOUND_CLI.startswith(("336", "337")):
    print("WARNING: NEXMO_OUTBOUND_CLI", NEXMO_OUTBOUND_CLI,
          "is a French mobile range; transfers to France will be rejected. "
          "Set NEXMO_OUTBOUND_CLI to a non-336/337 Vonage number.")

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


def send_vonage_sms(to, text):
    """Send an SMS via Vonage with an explicit timeout.

    The nexmo library hardcodes timeout=None on its requests session, so a
    slow API call blocks forever. Even on a background thread that leaks a
    thread per call, so bound it here.
    """
    resp = requests.post(
        "https://rest.nexmo.com/sms/json",
        data={
            "api_key": NEXMO_API_KEY,
            "api_secret": NEXMO_API_SECRET,
            "from": (NEXMO_NUMBER or "").lstrip("+"),
            "to": (to or "").lstrip("+"),
            "text": text,
        },
        timeout=10,
    )
    body = resp.json()
    status = body.get("messages", [{}])[0].get("status")
    if status != "0":
        print("Vonage SMS rejected:", body)
    return status == "0"


def announce_vfa_call(them, conv_id, language, recipient):
    """Log the call and text the volunteer. Runs off the request path."""
    log_comm(
        provider='Nexmo',
        comm_type='Call',
        direction='Inbound',
        from_num=them,
        to_num=NEXMO_NUMBER,
        sid=conv_id,
        content=f"VFA call routing in {language}. Sent to {recipient}.",
    )
    send_vonage_sms(
        recipient,
        f"VFA voter-help call from {them}. Language: {language}",
    )

@app.route("/admin/history")
@requires_auth
def admin_history():
    db_error = None
    try:
        logs = CommunicationLog.query.order_by(
            CommunicationLog.timestamp.desc()).limit(100).all()
    except Exception as e:
        db_pg.session.rollback()
        logs = []
        db_error = f"Could not read the database: {type(e).__name__}: {e}"

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
        {% if db_error %}
        <div style="padding:.75rem;background:#fff3cd;color:#664d03;
                    border:1px solid #ffecb5;border-radius:6px;margin-bottom:1rem;">
            <strong>Note:</strong> {{ db_error }}
        </div>
        {% endif %}
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
            {% set play_url = "/play_nexmo?url=" + log.recording_url if log.provider == 'Nexmo' else log.recording_url %}
            <audio controls src="{{ play_url }}" style="margin-top:10px;"></audio>
            <div style="margin-top: 5px;">
                <a href="{{ play_url }}" target="_blank" style="font-size: 0.8rem; color: #0077ff;">Open Recording</a>
            </div>
        {% endif %}
        
        </div>
<div class="time">
    {% set local_time = log.timestamp | paris_time %}
    {{ local_time.strftime('%Y-%m-%d') }}<br>
    {{ local_time.strftime('%H:%M:%S') }}
</div>
</div>
{% endfor %}
    </div>
</body>
</html>
    """, logs=logs, db_error=db_error)

@app.route("/answer", methods=["GET", "POST"])
@csrf.exempt 
def nexmo_answer():
    """Initial IVR Greeting"""
    receive_numbers = f"{request.url_root.rstrip('/')}/language"
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

    # Vonage's own webhooks do not carry the original caller past this point
    # (the connect leg's "from" is our Vonage number, not theirs), and the
    # Postgres row we write below may not exist if the free tier is
    # exhausted. So thread the caller through the callback URLs themselves.
    caller_qs = f"?caller={quote(str(them))}"

    if digits == "2":
        route_url = f"{request.url_root.rstrip('/')}/voicemail_french{caller_qs}"
        language = 'french'
    else:
        route_url = f"{request.url_root.rstrip('/')}/voicemail_english{caller_qs}"
        language = 'english'

    recipient = choose_recipient()

    caller_digits = str(them or "").lstrip("+")
    if NEXMO_USE_CALLER_CLI and caller_digits.isdigit():
        connect_from = caller_digits
    else:
        connect_from = NEXMO_OUTBOUND_CLI
    print("Nexmo connect: from", connect_from, "-> to", recipient,
          "(caller CLI passthrough %s)" % ("ON" if NEXMO_USE_CALLER_CLI else "off"))

    # Vonage drops the caller if this webhook takes more than ~5s. The log
    # write can block waking a sleeping Postgres, and the SMS is a second
    # network round trip, so neither runs before we return the NCCO.
    run_in_background(
        announce_vfa_call,
        them=them,
        conv_id=conv_id,
        language=language,
        recipient=recipient,
    )

    return jsonify([
        {
            "action": "connect",
            "timeout": 10,  # integer, not "10": Vonage types this as a number
            "from": connect_from,
            "eventType": "synchronous",  
            "eventUrl": [route_url],
            "endpoint": [{"type": "phone", "number": recipient.lstrip('+')}]
        },
            {
                "action": "stream",
                "streamUrl": [ENGLISH] if language == 'english' else [FRENCH]
            },
            {
                "action": "record",
                "beepStart": True,
                "eventUrl": [f"{request.url_root.rstrip('/')}/new-recording{caller_qs}"],
                "endOnSilence": 3
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
    # The caller rides in on the URL we built, so this email survives the
    # database being asleep, exhausted or unreachable. Postgres is only the
    # fallback now, and it is consulted only when the URL did not carry one.
    caller_id = request.args.get('caller') or None
    to_num = NEXMO_NUMBER
    if not caller_id:
        original_call = find_comm(sid=conv_id)
        if original_call:
            caller_id = original_call.from_num
            to_num = original_call.to_num
    caller_id = caller_id or "Unknown Caller"

    if not recording_url:
        return "", 204

    log_comm(
        provider='Nexmo',
        comm_type='Call',
        direction='Inbound',
        from_num=caller_id,
        to_num=to_num,
        sid=conv_id,                # SAVE THIS so it doesn't show as None!
        content='New Voicemail',
        recording_url=recording_url 
    )

    proxy_link = f"{request.url_root.rstrip('/')}/play_nexmo?url={recording_url}"
    
    subject = f"New VFA Nexmo Voicemail from {caller_id}"
    html = f"<p>Voicemail from: {caller_id}</p><p><a href='{proxy_link}'>CLICK HERE TO LISTEN TO RECORDING</a></p>"
    send_email(FROM_EMAIL, RECIPIENT_EMAILS, subject, html)
    
    return "", 204

@app.route("/nexmo-status", methods=["POST"])
@csrf.exempt
def nexmo_status():
    data = request.get_json()
    print(f"CALL STATUS UPDATE: {data.get('status')} for {data.get('to')}")
    if data.get('status') == 'failed':
        print(f"REASON: {data.get('detail')}")
    return "", 204

def connect_event_status():
    """Log the Vonage connect event and return its status.

    Vonage calls a synchronous connect eventUrl when the volunteer's leg
    reaches a terminal state (failed, rejected, unanswered, busy, timeout),
    and whatever NCCO we return replaces the rest of the call. We were
    discarding this payload, which is the only place Vonage says *why* the
    connect failed.
    """
    data = request.get_json(silent=True) or {}
    print("CONNECT EVENT:", json.dumps(data, default=str))
    status = str(data.get('status') or '').lower()

    if status in ("rejected", "failed"):
        run_in_background(
            alert_transfer_failure,
            provider="Vonage",
            summary=f"{status} ({data.get('detail') or 'no detail'}) "
                    f"dialling {data.get('to')}",
            details=json.dumps(data, indent=2, default=str),
        )

    return status


@app.route("/voicemail_english", methods=["POST"])
@csrf.exempt
def voicemail_english(): 
    status = connect_event_status()
    if status in ("started", "ringing", "answered"):
        # The volunteer's phone is still ringing or already answered. Sending
        # a voicemail NCCO here would cut them off mid-connect.
        print("Nexmo: connect still in progress (%s); not interrupting" % status)
        return "", 204

    print("Nexmo: Entering English Voicemail after connect status:", status)
    caller = request.args.get('caller', '')
    newrecording = f"{request.url_root.rstrip('/')}/new-recording?caller={quote(caller)}"
    
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
    status = connect_event_status()
    if status in ("started", "ringing", "answered"):
        # The volunteer's phone is still ringing or already answered. Sending
        # a voicemail NCCO here would cut them off mid-connect.
        print("Nexmo: connect still in progress (%s); not interrupting" % status)
        return "", 204

    print("Nexmo: Entering French Voicemail after connect status:", status)
    caller = request.args.get('caller', '')
    newrecording = f"{request.url_root.rstrip('/')}/new-recording?caller={quote(caller)}"
    
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
    log_comm(
        provider='Nexmo',
        comm_type='SMS',
        direction='Inbound',
        from_num=them,
        content=msg
    )

    send_email(FROM_EMAIL, RECIPIENT_EMAILS, f"Nexmo SMS from {them}", f"Body: {msg}")
    return "", 204
