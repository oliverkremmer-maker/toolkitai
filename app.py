import os, sqlite3, uuid, json, time
from flask import Flask, render_template, request, jsonify, redirect, make_response
from openai import OpenAI
import stripe

app = Flask(__name__)

# ── Config (set via environment variables) ─────────────────────────────────────
OPENAI_API_KEY        = os.environ.get("OPENAI_API_KEY", "")
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID       = os.environ.get("STRIPE_PRICE_ID", "")        # monthly sub price ID
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
BASE_URL              = os.environ.get("BASE_URL", "http://localhost:5000")

stripe.api_key = STRIPE_SECRET_KEY
openai_client  = OpenAI(api_key=OPENAI_API_KEY)

# ── Database ───────────────────────────────────────────────────────────────────
DB = "users.db"

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            token TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )""")

def get_user(token):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT * FROM users WHERE token=? AND active=1", (token,)).fetchone()
    return row

def create_user(token, cid, sid):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO users (token,stripe_customer_id,stripe_subscription_id) VALUES (?,?,?)",
                  (token, cid, sid))

def deactivate_sub(sid):
    with sqlite3.connect(DB) as c:
        c.execute("UPDATE users SET active=0 WHERE stripe_subscription_id=?", (sid,))

# ── Auth helper ────────────────────────────────────────────────────────────────
def get_token():
    return request.cookies.get("tk") or request.args.get("tk", "")

def auth_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*a, **kw):
        if not get_user(get_token()):
            return redirect("/")
        return fn(*a, **kw)
    return wrapped

# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = {
    "cover_letter": {
        "name": "Cover Letter Writer",
        "icon": "✉️",
        "desc": "Tailored cover letter from a job description in seconds.",
        "fields": [
            {"id":"job_title",   "label":"Job Title",           "type":"text",     "placeholder":"Senior Product Manager"},
            {"id":"company",     "label":"Company Name",        "type":"text",     "placeholder":"Acme Corp"},
            {"id":"job_desc",    "label":"Job Description",     "type":"textarea", "placeholder":"Paste the full job description…"},
            {"id":"experience",  "label":"Your Key Experience", "type":"textarea", "placeholder":"3 bullet points about your relevant background…"},
        ],
        "prompt": lambda f: f"""Write a compelling, professional cover letter for the following role.

Job Title: {f['job_title']}
Company: {f['company']}
Job Description: {f['job_desc']}
Applicant's Key Experience: {f['experience']}

Guidelines:
- 3 paragraphs, under 350 words
- Opening: hook that shows genuine knowledge of the company
- Middle: connect their needs to the applicant's experience (cite specific achievements)
- Close: confident CTA, no "I look forward to hearing from you" clichés
- Tone: warm, confident, human — not stiff or corporate
- Do NOT use phrases like "I am writing to apply" or "I am excited to"
Output only the letter text, no preamble."""
    },
    "cold_email": {
        "name": "Cold Email Sequence",
        "icon": "📨",
        "desc": "3-email outreach sequence that actually gets replies.",
        "fields": [
            {"id":"prospect_name",    "label":"Prospect Name",    "type":"text",     "placeholder":"Sarah Chen"},
            {"id":"prospect_company", "label":"Their Company",    "type":"text",     "placeholder":"Acme Corp"},
            {"id":"prospect_role",    "label":"Their Role",       "type":"text",     "placeholder":"Head of Operations"},
            {"id":"your_offer",       "label":"What You're Offering", "type":"textarea","placeholder":"A 30-min strategy call about reducing their onboarding time…"},
            {"id":"goal",             "label":"Goal of Sequence", "type":"text",     "placeholder":"Book a 30-minute discovery call"},
        ],
        "prompt": lambda f: f"""Write a 3-email cold outreach sequence.

Prospect: {f['prospect_name']}, {f['prospect_role']} at {f['prospect_company']}
Offer: {f['your_offer']}
Goal: {f['goal']}

Rules:
- Email 1: Pattern interrupt opener, one specific insight about their company/role, soft CTA. Max 100 words.
- Email 2 (3 days later): Different angle, add social proof or a relevant result, one question CTA. Max 80 words.
- Email 3 (5 days later): Short break-up email, a little humour, final easy CTA. Max 60 words.
- No buzzwords: synergy, leverage, circle back, touch base
- Each email needs a compelling subject line

Format each email clearly as:
EMAIL 1 — Subject: [subject]
[body]

EMAIL 2 — Subject: [subject]
[body]

EMAIL 3 — Subject: [subject]
[body]"""
    },
    "meeting_summarizer": {
        "name": "Meeting Summarizer",
        "icon": "📋",
        "desc": "Paste raw notes or a transcript. Get a clean summary + action items.",
        "fields": [
            {"id":"meeting_title", "label":"Meeting Title (optional)", "type":"text",     "placeholder":"Q3 Planning Call"},
            {"id":"transcript",    "label":"Notes / Transcript",       "type":"textarea", "placeholder":"Paste your meeting notes or transcript here…"},
        ],
        "prompt": lambda f: f"""Analyse this meeting content and produce a structured summary.

Meeting: {f.get('meeting_title','Untitled Meeting')}
Content:
{f['transcript']}

Output format (use these exact headers):

## Summary
2-3 sentences capturing the core purpose and outcome of the meeting.

## Key Decisions
Bullet list of decisions made (if none, write "None").

## Action Items
For each action item: [Owner if mentioned] — [Task] — [Deadline if mentioned]

## Open Questions
Things raised but not resolved (if none, write "None").

Be concise and factual. Do not invent information not present in the notes."""
    },
    "linkedin_bio": {
        "name": "LinkedIn Bio Builder",
        "icon": "💼",
        "desc": "An About section that positions you perfectly for your target role.",
        "fields": [
            {"id":"current_role",    "label":"Current Role & Company",    "type":"text",     "placeholder":"Head of Growth at TechStartup"},
            {"id":"experience",      "label":"Career Highlights (3-5)",   "type":"textarea", "placeholder":"Scaled revenue from £0 to £2M…\nBuilt a team of 12…"},
            {"id":"target_audience", "label":"Who Reads Your Profile",    "type":"text",     "placeholder":"Series A/B founders, hiring managers at SaaS companies"},
            {"id":"tone",            "label":"Tone",                      "type":"text",     "placeholder":"Confident but approachable, no corporate speak"},
            {"id":"goal",            "label":"What You Want Readers to Do","type":"text",    "placeholder":"Reach out for consulting, or consider me for Head of Growth roles"},
        ],
        "prompt": lambda f: f"""Write an optimised LinkedIn About section.

Current Role: {f['current_role']}
Career Highlights: {f['experience']}
Target Audience: {f['target_audience']}
Tone: {f['tone']}
Goal: {f['goal']}

Rules:
- Start with a punchy 1-2 sentence hook (no "I am a passionate…" openings)
- 3-4 short paragraphs, max 300 words total
- Quantify achievements where possible
- End with a clear, low-friction CTA
- Write in first person
- LinkedIn renders plain text — no markdown, just line breaks between paragraphs
- Make it sound like a human wrote it, not a template"""
    },
    "proposal_writer": {
        "name": "Proposal Writer",
        "icon": "📄",
        "desc": "Full client proposal from a project brief.",
        "fields": [
            {"id":"client_name",   "label":"Client Name",        "type":"text",     "placeholder":"Acme Corp"},
            {"id":"project",       "label":"Project Description", "type":"textarea", "placeholder":"Redesign their e-commerce checkout flow to reduce abandonment…"},
            {"id":"approach",      "label":"Your Approach",       "type":"textarea", "placeholder":"Discovery week → wireframes → 2 rounds of design → handoff to dev…"},
            {"id":"timeline",      "label":"Timeline",            "type":"text",     "placeholder":"6 weeks"},
            {"id":"investment",    "label":"Investment / Price",  "type":"text",     "placeholder":"£4,500"},
            {"id":"your_name",     "label":"Your Name / Company", "type":"text",     "placeholder":"Jane Smith / Smith Studio"},
        ],
        "prompt": lambda f: f"""Write a professional client proposal document.

Client: {f['client_name']}
Project: {f['project']}
Approach: {f['approach']}
Timeline: {f['timeline']}
Investment: {f['investment']}
Prepared by: {f['your_name']}

Structure the proposal with these sections:
1. Executive Summary (2-3 sentences, outcome-focused)
2. The Challenge (restate their problem in a way that shows deep understanding)
3. Our Approach (expand the approach provided, broken into clear phases)
4. Timeline (present the timeline clearly, with phases if applicable)
5. Investment (present the price confidently, include what's included)
6. Why Us (2-3 sentences on credibility/fit — keep it brief)
7. Next Steps (clear, simple CTA — e.g. "Reply to this proposal to confirm" or "Sign the attached agreement")

Tone: Confident, clear, professional. No fluff. Write as if this is a real deliverable being sent to a client today."""
    },
    "email_rewriter": {
        "name": "Email Rewriter",
        "icon": "✏️",
        "desc": "Paste any email. Get a sharper, more effective version.",
        "fields": [
            {"id":"original_email", "label":"Original Email",  "type":"textarea", "placeholder":"Paste the email you want to improve…"},
            {"id":"tone",           "label":"Desired Tone",     "type":"text",     "placeholder":"Professional but warm / Direct / Formal"},
            {"id":"goal",           "label":"What Should It Achieve","type":"text","placeholder":"Get a reply confirming the meeting / Politely decline / Close the deal"},
        ],
        "prompt": lambda f: f"""Rewrite the following email to be sharper and more effective.

Original email:
{f['original_email']}

Desired tone: {f['tone']}
Goal: {f['goal']}

Rules:
- Keep it as short as possible while hitting the goal
- Cut anything that doesn't serve the goal (pleasantries, filler, repetition)
- Subject line should be specific, not generic
- One clear ask or CTA per email
- Output format:

Subject: [subject line]

[rewritten email body]

Then below, in italics, add 2-3 bullet notes on the key changes you made and why."""
    },
    "job_decoder": {
        "name": "Job Description Decoder",
        "icon": "🔍",
        "desc": "Decode any job listing: real requirements, red flags, salary intel, interview prep.",
        "fields": [
            {"id":"job_description", "label":"Job Description", "type":"textarea", "placeholder":"Paste the full job description here…"},
        ],
        "prompt": lambda f: f"""Analyse this job description thoroughly.

{f['job_description']}

Produce the following analysis:

## Must-Have Requirements
The 5-7 skills/experiences they truly cannot hire without.

## Nice-to-Have (Don't Stress These)
Requirements that are aspirational — missing them won't disqualify you.

## Red Flags
Anything in the description that suggests a difficult culture, unrealistic expectations, or poor management. Be direct.

## What They're Really Looking For
In 2-3 sentences, describe the actual type of person they want — beyond the bullet points.

## Salary Estimate
Based on the role, level, and any clues in the description — provide a realistic range. Flag if no useful signals are present.

## 5 Interview Questions to Prepare
The most likely questions for this specific role, based on the requirements listed.

## Keywords to Mirror
5-7 phrases from the JD to reflect in your CV and cover letter for ATS optimisation."""
    },
}

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", tools=TOOLS)

@app.route("/dashboard")
@auth_required
def dashboard():
    return render_template("dashboard.html", tools=TOOLS)

@app.route("/access")
def access():
    """After Stripe payment — set cookie and redirect to dashboard."""
    token = request.args.get("tk", "")
    if not token or not get_user(token):
        return redirect("/")
    resp = make_response(redirect("/dashboard"))
    resp.set_cookie("tk", token, max_age=60*60*24*365, httponly=True, samesite="Lax")
    return resp

@app.route("/api/generate", methods=["POST"])
@auth_required
def generate():
    data = request.get_json()
    tool_id = data.get("tool")
    fields  = data.get("fields", {})

    if tool_id not in TOOLS:
        return jsonify({"error": "Unknown tool"}), 400

    tool   = TOOLS[tool_id]
    prompt = tool["prompt"](fields)

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert professional writing assistant. Produce output that is polished, specific, and immediately usable. Never add meta-commentary like 'Here is your...' — output the deliverable directly."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.7,
            max_tokens=1500,
        )
        return jsonify({"result": resp.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Stripe routes ───────────────────────────────────────────────────────────────

@app.route("/checkout", methods=["POST"])
def checkout():
    try:
        token = str(uuid.uuid4())
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{BASE_URL}/access?tk={token}",
            cancel_url=f"{BASE_URL}/",
            metadata={"user_token": token},
        )
        return redirect(session.url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "Bad signature", 400

    if event["type"] == "checkout.session.completed":
        s   = event["data"]["object"]
        tok = s.get("metadata", {}).get("user_token", str(uuid.uuid4()))
        create_user(tok, s.get("customer"), s.get("subscription"))

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        if sub.get("status") not in ("active", "trialing"):
            deactivate_sub(sub["id"])

    return "ok", 200

# ── Boot ───────────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
