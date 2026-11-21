# Oscar

A miniature voice-agent platform, modelled on Elphie. Oscar answers and places
voice calls with an AI agent that **listens** and **speaks**, and it
has a simple dashboard for talking to the agent, changing how it behaves and
reading past calls.

- **Browser calls:** click *Start call* and talk through your microphone.
- **Phone calls:** call your Twilio number, or have Oscar dial any number.
- **Web search:** ask for the weather, the news or a price, on a call or in
  WhatsApp, and Oscar looks it up (with a Firecrawl key).
- **WhatsApp:** add Oscar's number to a group and ask it things with *@oscar*.
- **Scripts:** write the steps of a call and Oscar works through them one at a
  time, coming back to its step after a side question.
- **Call summaries:** when a call ends, Oscar writes what it was about, the
  caller's name and number if they left them, and what is left for you to do.
- **Research notes:** what Oscar's web searches on the call found, written
  down with links to the pages.
- **Dashboard:** live transcript, agent settings, call history with summaries
  and transcripts.

One OpenRouter key runs all of it, bar web search (on calls and in WhatsApp),
which needs a Firecrawl key:

| Job | Service |
|---|---|
| Calls: hearing and speaking | OpenAI `gpt-audio`, a speech-to-speech model, through OpenRouter |
| Calls: the written transcript, and Oscar's memory of the caller | Deepgram Nova-3 in its several-languages mode, through OpenRouter |
| Calls: keeping Oscar's place in a script | Claude Haiku, through OpenRouter |
| Calls: the summary and the research notes afterwards | Claude Haiku, through OpenRouter |
| Web search (optional) | Firecrawl finds the pages. On calls Claude Haiku reads them; in WhatsApp the chat model does |
| WhatsApp replies | Any OpenRouter model (default `anthropic/claude-haiku-5.5`) |
| Voice pipeline | Pipecat 1.15 (voice activity detection and smart turn-taking) |
| Phone line | Twilio Media Streams |

## Requirements

- Python 3.13 (`brew install python@3.13`) and [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- An [OpenRouter](https://openrouter.ai/keys) API key with some credit on it
- For web search only: a [Firecrawl](https://firecrawl.dev) API key
- For phone calls only: a [Twilio](https://console.twilio.com) account with a phone number, and [ngrok](https://ngrok.com) (or another tunnel)

## Setup

```bash
cd ~/Desktop/Oscar
```

```bash
uv venv --python 3.13 .venv
```

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

Without uv: `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

Save your keys. This asks for each without showing it on screen and writes it
to `.env`. The OpenRouter key is needed; press Enter at the Firecrawl prompt to
go without web search:

```bash
.venv/bin/python -m scripts.set_keys
```

Or do it by hand: `cp .env.example .env`, then fill in `OPENROUTER_API_KEY`
(and `FIRECRAWL_API_KEY`, if you want web search).

## Run

```bash
.venv/bin/python -m app
```

Open <http://127.0.0.1:7860> and click **Start call**. Allow microphone access
when the browser asks. The agent greets you, then just talk. Click **End call**
when you are done.

### Start at login (Mac)

To have Oscar start when you log in to your Mac and stay running, run this
once:

```bash
.venv/bin/python -m scripts.autostart
```

macOS then starts Oscar at every login and starts it again if it ever stops,
so the dashboard is always at <http://127.0.0.1:7860>. There is no need to run
`python -m app` yourself any more (it would find the port taken).

| Command | What it does |
|---|---|
| `.venv/bin/python -m scripts.autostart --status` | Says whether it is set up, running and answering |
| `.venv/bin/python -m scripts.autostart --restart` | Restarts Oscar. Needed after changing its code or `.env` (new keys); settings saved on the dashboard need no restart |
| `.venv/bin/python -m scripts.autostart --remove` | Stops Oscar and no longer starts it at login |

- **A call in progress is not cut off.** `--restart` and `--remove` stop and
  say so if Oscar is on a call. End the call first, or add `--force`.
- **The log** is `~/Library/Logs/Oscar/oscar.log`, readable only by you. A
  short call adds about 12 KB to it, and nothing is added between calls.
- **Oscar runs while you are logged in and the Mac is awake.** A sleeping Mac
  (lid closed) answers nothing until it wakes.
- **If you move or rename the Oscar folder,** run the first command again.
- It is a launch agent: `~/Library/LaunchAgents/local.oscar.server.plist`.
  When checked on 2026-11-13, Oscar answered two seconds after being started
  this way, was back one second after its process was stopped, and answered a
  real call as fast as usual (2.1 seconds).

**Speakers or headphones.** On speakers, Oscar's voice can leak back into your
microphone, and it would cut itself off and answer its own words. So by default
Oscar finishes speaking before it listens: anything said while it is talking is
not heard. On headphones, switch on **I'm on headphones** under the Start call
button and you can interrupt it mid-sentence. Callers on the phone can always
interrupt.

**Web search.** With a Firecrawl key in `.env`, the top of the dashboard says
**Web search: on** and Oscar looks things up when a caller asks for something
current: the weather, the news, a price, a score, opening hours. You hear "One
moment, let me look that up", then the answer, and the transcript notes what was
searched for. On fourteen real calls on 11 November 2026 the "one moment" line
usually began about two seconds after the caller stopped (1.1 seconds at best,
6.2 at worst), and the answer five to seven seconds after the caller stopped
(4.4 to 8.7). Questions that need no search are answered as fast as before. On
speakers, Oscar does not listen while it searches, just as it does not while it
speaks.

**Searches build on one another.** What each search finds is kept for the rest
of the call, so a later question can lean on an earlier one:

- After "find the listing at 7747 Via Grande", the question "who is the listing
  agent for that property?" is searched for with the address in it, and "who
  owns it?" after that. The answer is written knowing the call so far and what
  the earlier searches found, so it does not contradict what Oscar already
  said.
- When the first results name what the question is about but not the answer,
  Oscar searches once more in the same turn. You hear "Still looking, one more
  moment." while it does. A turn never takes more than two searches.
- A follow-up Oscar has already answered ("what was the address again?") is
  answered from the call, without another search.

On a real four-turn call about one property on 13 November 2026 every search
carried the address, and the turn that needed a second search took about
seventeen seconds from the question to the end of the answer.

A search costs two Firecrawl credits and a little under a cent on OpenRouter on
top of a normal reply (a check call that searched cost 2.6 cents in all, one
that did not 1.8). Firecrawl's plan limits how many searches a minute it takes
(11 on the plan seen here): past that, Oscar says it could not look it up.

Without a Firecrawl key Oscar cannot look anything up during a call, and it is
told to say so rather than pretend.

When you stop talking, your whole turn goes to the model as audio and its
spoken answer comes straight back: one round trip. Expect just under two
seconds before each reply (1.7 to 2.0 seconds on four real calls on 11 November
2026; nearly all of it is the model's own time to start speaking). If you carry
on talking before Oscar answers, it hears everything you said as one piece.

The greeting plays as soon as the call connects. It is the same words each
time, so it is recorded on the first call and kept in `data/recordings/` (as is
the "one moment" line), and recorded again after you change the greeting, the
voice, the accent or the instructions. Asked for on every call, it took 1.8
seconds to start; recorded, 0.35 seconds after the call was placed (19 November
2026, on this Mac).

Oscar's log has one line for each reply, saying how long it took to be heard
and where the time went, for example `Turn 3: heard after 1.19s (answer began
1.04s, lookup decided 1.19s)`. The time counts from when Oscar judged your turn
over, about 0.2 seconds after you stopped. With web search on, the reply waits
for the decision whether to search: a decision later than "answer began" is
time spent waiting for it (0 to 0.7 seconds on calls measured on 19 November
2026). On a server, `pm2 logs oscar` shows these lines.

OpenRouter has no live two-way audio connection, which is what the fastest
voice agents use (well under a second). Getting there would mean a second key,
straight from a provider that offers one.

**What calls cost** (from what OpenRouter billed for real answers on 11 November
2026): about 8 cents for each minute Oscar speaks and about 2 cents for each
minute the caller speaks. A five-minute conversation comes to roughly 20 to 25
cents. `VOICE_MODEL` in `app/speech.py` can be set to `openai/gpt-audio-mini`,
which costs a small fraction of that and answers about 0.7 seconds later.

### Spending

**Call history** shows what each call cost and, above the table, what calls
cost in the last 24 hours and the last 30 days. Oscar adds up what OpenRouter
reports for each request a call makes: its answers, the transcripts, the
lookups, and the summary and research notes written afterwards. An answer cut
off part-way (you carried on talking) is looked up by its id 10 to 15 seconds
later. A request cut off before OpenRouter answered at all can't be looked up,
so a call's cost is a close account rather than an exact one: on test calls
on 20 November 2026 it came to 80% of what OpenRouter billed, on a connection
so slow that nearly every request was cut off. Calls from before this was
added show "—".

The exact figure is OpenRouter's own, for the key: the dashboard shows what is
left of the key's limit (also in the top bar as **Key: $… left**) and what the
key spent today and this month. That counts everything that uses the key:
WhatsApp too, and any other app given the same key. To see Oscar on its own,
give it a key of its own.

**A spending cap.** Set it on the key itself, on openrouter.ai under **Keys**:
a credit limit, which OpenRouter can reset daily, weekly or monthly.
OpenRouter enforces it, so it can't be overspent. Oscar reads it and, once
less than 5 cents is left, starts no new call. A browser call or a call you
place says why, and someone phoning in hears "Sorry, this agent cannot take
calls right now" and the call shows in **Call history** with the reason. A
call already going carries on.

## Run it on a server (pm2)

Oscar can also run on a Linux server, such as an EC2 machine, under pm2. Then it
answers whether or not your Mac is awake. `deploy.sh` sets it up and brings it
up to date, in one command.

**Once, on the server** (Ubuntu). Install git, Node.js with pm2, uv (which
brings Python 3.13) and libmagic (for WhatsApp):

```bash
sudo apt update && sudo apt install -y git nodejs npm libmagic1 && sudo npm install -g pm2 && curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then get Oscar and start it:

```bash
git clone https://github.com/theshreyanshsingh/oscar.git ~/oscar && cd ~/oscar && ./deploy.sh
```

Add your keys, then restart Oscar so it reads them (and its WhatsApp
connection, if it runs here: it uses the same key):

```bash
.venv/bin/python -m scripts.set_keys && pm2 restart ecosystem.config.cjs
```

For WhatsApp too, run `./deploy.sh --whatsapp` once. Then link Oscar's phone
from the dashboard's WhatsApp card, as in [Set up WhatsApp](#set-up-whatsapp).

**Every update:** push to GitHub, then on the server:

```bash
cd ~/oscar && ./deploy.sh
```

It stops if Oscar is on a call and changes nothing (`--force` cuts the call
off). Otherwise it:

1. Gets the latest code.
2. Installs Python packages only when a requirements file changed.
3. Checks that the new code starts. If it doesn't, or its packages could not
   be installed, it goes back to the version before, and Oscar keeps running
   that.
4. Restarts Oscar under pm2 and waits until it answers. If a call started
   while it was busy, it leaves Oscar running instead: the new code is in place,
   and the next `./deploy.sh` starts it.

The first time, it also makes pm2 start Oscar when the server starts, and has
pm2 rotate its logs so they never fill the disk. When checked on 17 November
2026 (on a test copy, without keys), a first deploy took 1 minute 44 seconds,
the next one 15 seconds. A broken commit was turned back, and Oscar answered
throughout.

| On the server | What it does |
|---|---|
| `pm2 ls` | Shows whether `oscar` (and `oscar-whatsapp`) are running |
| `pm2 logs oscar` | Oscar's log as it is written (`--lines 100` for more) |
| `pm2 restart ecosystem.config.cjs` | Restarts Oscar and its WhatsApp connection, after a change to the keys in `.env` (both read them). Unlike `deploy.sh`, it cuts off a call in progress |
| `pm2 restart oscar` | Restarts only Oscar, after a change to its other settings in `.env` (such as `HOST`). It cuts off a call in progress too |

- **The dashboard only listens on the server itself,** unless `.env` says
  otherwise. Reach it from your Mac through SSH, then open
  <http://127.0.0.1:7860> there (the browser allows the microphone on that
  address):

  ```bash
  ssh -L 7860:127.0.0.1:7860 ubuntu@<server address>
  ```

- **Or open it at the server's own address,** `http://<server address>:7860`:
  set `HOST=0.0.0.0` in `.env`, run `pm2 restart oscar`, and allow TCP 7860
  in the firewall. There is no password, so put Cloudflare Access in front
  (below), or allow the port from your own IP only. Browsers only let a page
  use the microphone over https or on localhost, so for **Start call** use the
  SSH address above, or put https in front.
- **Browser calls send their audio straight to the server over UDP,** not
  through SSH or the web address (a proxy in front, such as Cloudflare, does
  not carry it). Let in **UDP 32768-60999** in the firewall from wherever you
  call from: on EC2, an inbound Custom UDP rule in the security group. Those
  are the ports Linux hands out for each call's audio; `deploy.sh` and Oscar's
  log say the server's own range. Without it a call fails with "The call's
  audio could not reach Oscar's server". Phone calls don't need this.
- **Protect the dashboard** before anyone else can reach it. See below.
- **Phone calls** need `PUBLIC_URL` to be a public https address that reaches
  the server, such as a domain behind Caddy or nginx. See below.
- **Run WhatsApp in one place only.** If Oscar is linked on your Mac and on the
  server, it answers twice in groups. Once the server runs it, stop the Mac's
  copy with Ctrl+C in its terminal: `scripts.autostart` runs only the
  dashboard, not WhatsApp.
- **Each machine keeps its own `data/`** (call history, settings, the WhatsApp
  login). It is not in git, so a new server starts empty.
- On a Mac, `deploy.sh` refuses to run where Oscar already starts at login. Use
  `scripts.autostart --restart` there.

### Protect the dashboard (Cloudflare Access)

Oscar has no password. Once its web address is public, anyone who finds it can
talk to Oscar on your OpenRouter credit, read every call's transcript (callers'
names and numbers too) and change the agent. If the address is on Cloudflare,
let Cloudflare Access decide who gets in: it asks for your email and sends a
one-time code to it, and Oscar then refuses every request that did not come
through that sign-in, including requests made straight to the server's own
address and port. Nothing in the firewall has to change.

1. **In Cloudflare:** open **Zero Trust**. The first time, pick a team name
   (your sign-in page becomes `<team name>.cloudflareaccess.com`) and the free
   plan.
2. Under **Access** (called **Access controls** in newer dashboards), open
   **Applications** → **Add an application** → **Self-hosted**:
   - **Application domain:** Oscar's web address, for example subdomain
     `oscar`, domain `example.com`, with no path.
   - **Policy:** action **Allow**, include **Emails**, and your email address
     (and anyone else who should get in).
   - **Login method:** **One-time PIN** is enough.
   - Save it.
3. Open the application again and copy its **Application Audience (AUD) Tag**.
4. **On the server,** add two lines to `~/oscar/.env`: the team domain and the
   tag.

   ```
   CF_ACCESS_TEAM_DOMAIN=<team name>.cloudflareaccess.com
   CF_ACCESS_AUD=<the Application Audience (AUD) Tag>
   ```

5. Then run `./deploy.sh` (or `pm2 restart oscar`). Its last lines say that
   the dashboard only opens for people signed in through Cloudflare Access.

Do steps 1 to 3 first. With the two lines in `.env` and no application in
Cloudflare yet, the dashboard turns everyone away, you too.

Then check:

- Oscar's web address asks for your email and a code, then opens the dashboard.
- `http://<server address>:7860` answers "This Oscar only opens through
  Cloudflare Access."

What stays open: `/healthz`, which only says Oscar is up and how many calls it
is on (`deploy.sh` asks it), and Twilio's webhooks under `/twilio/`, each
checked by Twilio's signature. **If you set up phone calls on this server,**
Twilio cannot sign in, so add a second application in Cloudflare for the same
domain with the path `twilio`, and a policy whose action is **Bypass** for
**Everyone**. Browser call audio does not go through Cloudflare at all, so it
needs nothing more.

`scripts.live_call_check` cannot sign in either, so run it on a computer where
Oscar is not behind Cloudflare Access.

## Phone calls (optional)

Twilio has to reach your computer over the internet, so phone calls need a
public https address.

1. Start a tunnel in a second terminal and copy the `https://…` address it shows:

   ```bash
   ngrok http 7860
   ```

2. Save your Twilio details. This asks for each one (the secret without
   showing it) and writes them to `.env`:

   ```bash
   .venv/bin/python -m scripts.set_keys --phone
   ```

   Then add the tunnel's address to `.env` by hand:

   ```
   PUBLIC_URL=https://your-tunnel.ngrok-free.app
   ```

   The dashboard has no password. Once `PUBLIC_URL` is set, anyone who has
   the tunnel's address can open it, so keep the address to yourself and stop
   the tunnel when you are done.

3. Restart Oscar. The dashboard's **Phone calls** card shows your incoming-calls webhook.

4. In the Twilio console, open your phone number. Under **Voice → A call comes
   in**, choose **Webhook**, paste the webhook URL (it ends in `/twilio/voice`)
   and select **HTTP POST**. Save.

Now call your Twilio number to talk to the agent, or type a number in
**Call a number** to have the agent call out. Trial Twilio accounts can only
call numbers you have verified in the Twilio console.

If your tunnel address changes (for example after restarting ngrok), update
`PUBLIC_URL`, restart Oscar, and update the Twilio webhook.

## WhatsApp (optional)

Oscar can be a member of your WhatsApp groups. It answers when someone:

- writes *@oscar* in a message, for example "@oscar can you get this" or
  "@oscar what are your thoughts on this?";
- picks Oscar in WhatsApp's @ list; or
- replies to one of Oscar's messages.

Oscar reads the recent messages in the chat, plus the message being replied
to. It can also see a photo that is sent with the question or replied to. With
a Firecrawl key (the same one calls use) it searches the web when a question
needs current information; you can turn that off on the dashboard. Every other message is only remembered as context,
and Oscar never posts on its own. Oscar's name and model come from **Agent
settings**, so if you rename it "Ava", people write *@ava*.

**Before you start, know the risk.** WhatsApp has no official way to add an AI
to a normal group: Meta's business API doesn't allow general-purpose AI
assistants (since January 2026), and only works in groups the business itself
creates. So Oscar connects the way WhatsApp Web does, through an unofficial
library. That breaks WhatsApp's terms of service, and WhatsApp can ban the
number. **Use a spare number for Oscar, never your own.**

**Not possible: group voice calls.** No available library can join WhatsApp
group calls with audio (the official API has no group calling either), so
Oscar cannot join them.

### Oscar on your own number

Oscar is meant to have a WhatsApp number to itself. If you only have your own
WhatsApp, link that and tick **Oscar shares my own number** on the dashboard
(under the WhatsApp card's chat settings). Oscar then behaves differently, so
that it never speaks for you by accident:

- It answers only when someone writes *@oscar* in a message. You can call it
  yourself, from your own phone.
- It also answers when someone picks a group member called Oscar from
  WhatsApp's @ list, and when someone replies to one of its answers.
- Every reply starts with ***Oscar:*** so nobody takes it for your own words.
- A message that mentions you, or replies to something you wrote, is left for
  you. Oscar never answers those.
- Private chats stay yours. With **Also reply to private messages** ticked,
  Oscar answers there too, but only messages that say *@oscar*.

The risk is larger than with a spare number: if WhatsApp bans the number, it
is yours. The WhatsApp process also receives every chat of that account while
it runs. It keeps recent messages in memory only, and sends a chat's recent
messages to the model only when Oscar is called in that chat.

### Set up WhatsApp

1. Get a spare phone number and set up WhatsApp on a phone with it. That phone
   is "Oscar's phone". It needs to come online at least every couple of weeks,
   or WhatsApp logs out linked devices.

2. Install the system library the WhatsApp connection needs:

   ```bash
   brew install libmagic
   ```

3. Create the WhatsApp environment. It is separate from `.venv` because the
   WhatsApp library and the voice pipeline need different versions of protobuf.

   ```bash
   uv venv --python 3.13 .venv-whatsapp
   ```

   ```bash
   uv pip install --python .venv-whatsapp/bin/python -r requirements-whatsapp.txt
   ```

4. Start the dashboard (`.venv/bin/python -m app`) and, in a second terminal,
   the WhatsApp connection:

   ```bash
   cd ~/Desktop/Oscar && .venv-whatsapp/bin/python -m app.whatsapp
   ```

5. The dashboard's **WhatsApp** card shows a code (also printed in the
   terminal). On Oscar's phone, open **WhatsApp → Settings → Linked devices →
   Link a device** and scan it. Scan with Oscar's phone, not your own:
   whichever WhatsApp account scans the code becomes Oscar. The code renews
   itself every 20 to 60 seconds for an hour, so there is no rush. The card
   then says **Connected** and shows the number that was linked. Check that it
   is Oscar's. You only link once: the login is saved in `data/whatsapp-session.sqlite3`. Keep that file
   private, because it holds the login keys.

6. On Oscar's phone, open **WhatsApp → Settings → Privacy → Groups** and
   choose **My contacts**. Then save your friends as contacts on that phone.
   Otherwise anyone who learns the number could add Oscar to their own groups
   and use it on your OpenRouter account.

7. From your own phone, add Oscar's number to a group, then write
   *@oscar hello*.

Keep the WhatsApp terminal running for Oscar to stay online. Stop it with
Ctrl+C; nothing is lost. To unlink Oscar, stop it and run
`.venv-whatsapp/bin/python -m app.whatsapp --unlink`, or remove it under
**Linked devices** on Oscar's phone.

**Limits:**

- Oscar answers at most 8 times per chat in 10 minutes and 60 times an hour
  overall, so a busy group can't run up your OpenRouter bill.
- It skips mentions that are more than 10 minutes old, for example ones that
  arrived while it was offline.
- It answers private messages only if you turn that on.
- Each web search costs two Firecrawl credits. One answer usually takes one
  search and can take nine at most. If there is no Firecrawl key, or the model
  you chose cannot use tools, Oscar answers without searching and says when
  something may be out of date.
- If the model you chose can't see photos, Oscar says so instead of guessing.
- It does not understand voice notes.
- It never looks at view-once photos.
- In chats with disappearing messages, its replies disappear too.
- It forgets messages that are deleted for everyone.

## What Oscar knows

Under **Agent** on the dashboard, **What Oscar knows** takes facts about
whoever Oscar answers for: opening hours, prices, address, services, how to
book. Write them as plain sentences, paste them in, or add the text of a
`.txt`, `.md` or `.csv` file (for a PDF or a Word file, copy the text). It
holds up to 12,000 characters.

- **On calls,** a question about you ("What time do you close on Saturday?")
  is answered from these facts. Oscar says "One moment, let me look that up."
  and the answer follows straight after. If the facts don't cover the question, it
  says it doesn't have that information, and adds how to reach you if the
  facts say how.
- **In WhatsApp,** the chat model is given the same facts with the same rule.
- **Phone numbers** are read out one digit at a time, exactly as you wrote
  them.
- **Left empty,** Oscar says it doesn't have the information whenever a caller
  asks about hours, prices, an address or bookings. It used to make these up.

The facts are not given to the model that speaks: they cost it the agent's
accent. Another model decides, for each thing the caller says, whether it is a
question about you; a text model writes the answer from the facts; the speaking
model says it word for word. That is the same route a web search takes, and it
needs no Firecrawl key.

## Script

Under **Agent** on the dashboard, **Script** takes the steps of a call, one on
each line, in the order Oscar is to take them. For example:

```
Ask who is calling.
Ask what they need help with.
Ask which day and time suit them.
Ask for the best number to reach them on, and repeat it back.
Say you will pass it all on, and ask if there is anything else.
Thank them and say goodbye.
```

- Oscar says its **Opening line** first, then takes the steps one at a time.
- **A side question** ("Is there parking nearby?") is answered, and Oscar then
  goes back to the step it was on. A web search or an answer from **What Oscar
  knows** counts as a side question: Oscar picks its step up on the next turn.
- **A caller who answers before being asked** ("It's Daniel, I'd like a haircut
  tomorrow at three") is not asked for the same thing again later. Oscar may
  still ask its next question once before it catches up.
- Write a step as an instruction to Oscar. For exact words, write them in the
  step: `Say: "Thanks for calling Harbour Street Barbers."`
- Numbers and bullets at the start of a line are ignored. Up to 4,000
  characters. Leave the box empty for no script.

How it is done: the speech model is not handed the whole script, because it
loses its place in one (it did the right step in 19 real replies of 32).
Before each of the caller's turns it is told the next step and the one after,
and a text model works out which step that is after every reply, while the
caller is still listening to it. Told that way, Oscar did the right single step
in 45 real replies of 46 and skipped none, and four whole test calls went
through every step in order.

Two things to know:

- **A script costs a little of the accent.** With one, 43 replies of 46 kept
  the Indian accent, against 80 of 80 without. Wordings that kept the accent
  every time skipped steps, which is the worse fault.
- **Working out the step takes about 2.5 seconds.** It is nearly always done
  before the caller has finished their next turn. When it is not, that turn
  waits up to 1.5 seconds for it.

## Call history

**Call history** on the dashboard lists calls newest first, 50 at a time:
**Show older calls** brings the next 50.

- **Search** finds calls by the caller's name, a number (typed any way:
  `0412 345 678` finds `+61412345678`), the summary, the to-do or anything said
  on the call.
- **Filters:** All, To do (with how many are still open), Incoming, Outgoing,
  Browser and Failed. They go together with a search.
- **To-dos** can be ticked off in the list or in the call itself. Writing a
  call's summary again starts its to-do afresh.
- **Opening a call** shows it in a panel beside the list: its summary, research
  notes, cost and transcript. Escape closes it. **Copy number** copies the
  number to call back on. With phone calls set up, **Call back** puts it in
  **Call a number**, so you can check it (and add the country code) before
  Oscar dials.

On a phone each call is a card, and the section links are under **Menu**.

## Call summaries

When a call ends, Oscar reads the transcript and writes a note for you. It
shows in **Call history**, under the live transcript of a call you just made,
and when you open a call:

- **Summary:** one or two sentences on what the caller wanted and what came of
  it.
- **Name** and **Call back on:** the caller's name and a number to reach them
  on, if they gave them.
- **To do:** what is left for you to do, such as calling someone back. Empty
  when the caller got what they needed.

It is one request to a text model after the call, so it never slows a reply.
It took one to two seconds and cost about a hundredth of a cent for a short
call when measured on 2026-11-12.

- **Numbers are checked.** A caller's number reaches the transcript as the
  words they said ("zero four two two double seven one..."). Oscar turns those
  into digits itself before the model reads them, and shows a number only if
  those digits were said on the call. A number it cannot match is left out, and
  the transcript still has it.
- **Older calls,** and calls whose summary could not be written, have a
  **Write a summary** button when you open them. **Write the summary again**
  replaces one you are not happy with.
- **A call where only Oscar spoke** is noted as that, without asking a model.
- A summary is a model's reading of the call. The transcript below it is the
  record.

### Summaries sent to you

Under **Call history**, **Send each summary to you** takes a webhook address.
When a call ends and its summary is written, Oscar posts it there, so it
arrives where you already look:

- **Slack:** create an incoming webhook (api.slack.com, *Incoming Webhooks*)
  and paste its `https://hooks.slack.com/…` address.
- **Discord:** a channel's *Integrations → Webhooks* address.
- **Google Chat:** a space's *Apps & integrations → Webhooks* address.
- **Zapier, Make, n8n or your own server:** a "catch hook" address. Besides the
  message, the post has the call's details as data: who called, when, how
  long, what it cost, the summary, the caller's name and number, the to-do and
  the research notes.

The message reads, for example:

```
Oscar's incoming call with +14155550123 (2:05, $0.031)
Summary: Sam asked for a quote for a new fence and left a number.
Name: Sam Taylor
Call back on: 0412345678
To do: Call Sam back about the quote.
```

**Send a test** posts a made-up summary so you can see it arrive, and the card
says whether the last one did. A webhook that fails for a moment is tried once
more three seconds later. Only `https://` addresses are taken. Not sent: calls
where only Oscar spoke, and summaries written again from the dashboard.

## Research notes

On a call Oscar has time to say a sentence or two of what a search found. The
pages it read are kept with the call, and when the call ends Oscar writes down
what they say about the things the caller asked for. Open a call in **Call
history** to see, under its summary:

- **Research:** up to ten notes, such as the price, size and agent of a
  listing. Each ends with the sites it comes from; click one to open the page.
- **Searches:** everything Oscar searched for on the call. Open one to see the
  pages it found.

Calls with notes say so in the history table ("6 research notes"), and the
notes also show under the live transcript once a call you made has ended.

- **It is one more request** to a text model after the call, beside the
  summary's. Measured on a real call of four searches on 2026-11-13, it took
  about six seconds and cost under a tenth of a cent.
- **Figures are checked.** A price, a year, a phone number or any other figure
  of three digits or more stays in a note only if it is on the pages that note
  links to, or was part of what the caller asked about (the street number in
  their question). A figure found on another of the call's pages adds that
  page to the note's links. A note with a figure found nowhere is left out.
  In trials on the pages of five real calls, 51 notes were written: none gave
  a figure that was on none of the pages, two had a page added, and one was
  left out for naming no page.
- **Names are not checked** the same way, so open the page before you rely on
  one. The notes are a model's reading of short excerpts, as a search engine
  shows them, not of the whole page.
- **Write the summary again** writes the notes again too.
- **Calls from before this was added** have no notes: the pages their searches
  found were not kept.

## Agent settings

Edit these on the dashboard. They apply from the next call.

- **Name:** the agent is told its name.
- **Voice:** one of the speech model's thirteen voices, eight men's and five
  women's. Cedar, a man's voice, is the default.
- **Accent:** Indian by default. Also Australian, British, Scottish, American,
  Southern American, Singaporean, Nigerian, French, German, or the voice's own
  (nearly all the voices sound American when left alone). The voices come
  without accents, so the model is asked to put one on. How well that holds
  depends on the voice, and the hint under Accent says how the voice in the
  form did, with a click to switch to a voice that held it every time:
  - *Indian:* Cedar had it on every reply of 80. Verse, Marin, Coral and Sage
    had it on 4 replies of 4; Alloy, Ash and Nova on 3; Shimmer on 2; Ballad,
    Echo, Fable and Onyx on 1, sounding British or American on the rest.
  - *Australian:* twelve of the thirteen voices take it; Ballad stays British.
  - *Added on 2026-11-22,* each tried on 7 replies (a greeting and an answer
    from Cedar and Marin, an answer from Verse, Coral and Ash), judged by three
    listening models that were not told the accent:
    - *Southern American:* held on 6 of 7; Coral sounded plain American. Replies
      can lean on Southern turns of phrase (one opened with "Howdy").
    - *French:* 4 of 7; Cedar and Marin every time, the others not.
    - *German:* 4 of 7; Marin and Verse every time, Cedar on 1 of 2.
    - *Singaporean:* 4 of 7; Cedar and Ash every time, Marin on 1 of 2.
    - *Nigerian:* 4 of 7; Cedar, Ash and Coral every time, Marin and Verse not.
    - *Scottish:* 3 of 7; Marin and Coral every time, Cedar, Verse and Ash not.

    Seven replies is a small trial, so hear a voice with **Hear this voice**
    before you save it. Tried and left out: Irish, New Zealand and South African
    (1 of 6 each), Filipino, Italian and Mexican (1 of 4; asked for a Mexican
    accent, Cedar answered in Spanish), Spanish (0 of 4), Canadian (always
    heard as American), and Kenyan and Jamaican (3 of 7, no voice more than
    once).

  The speech model has thirteen voices, and all of them are offered: OpenAI's
  service lists no others (checked on 2026-11-22).
- **Chat model:** the OpenRouter model that writes Oscar's WhatsApp replies
  (`anthropic/claude-haiku-5.5` by default). Calls do not use it: they always
  run on the speech model.
- **Opening line:** what Oscar says when a call connects. Leave it blank to let
  the caller speak first.
- **Call instructions:** the agent's system prompt for voice calls. WhatsApp
  has its own **Chat instructions** in the WhatsApp section.
- **Script:** the steps Oscar works through on a call. See the section above.
- **What Oscar knows:** facts about whoever Oscar answers for. See the
  section above.

**Hear this voice** (under Voice) plays about six seconds of Oscar saying hello
with the voice, accent and name in the form, before you save them. Each sample
is kept in `data/recordings/`, so hearing it again costs nothing; a new one
costs about a third of a cent. While there are unsaved changes, a bar with
**Save changes** and **Discard** stays in view, and the browser asks before you
leave the page.

## Checks

Automated tests run whole calls against a stand-in for OpenRouter, so they need
no keys or network. Install the test tools once:

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Then run the tests:

```bash
.venv/bin/python -m pytest
```

The WhatsApp connection has its own tests, run in its own environment:

```bash
uv pip install --python .venv-whatsapp/bin/python -r requirements-whatsapp-dev.txt
```

```bash
.venv-whatsapp/bin/python -m pytest tests_whatsapp
```

To check a real call with your keys: start the server, then in a second terminal run:

```bash
.venv/bin/python -m scripts.live_call_check
```

A recorded voice asks a question. The check passes when Oscar hears it and
speaks an answer back, and it prints how long the answer took to start. Each
run costs about two cents.

To check web search the same way (the voice asks for today's weather in Kanpur,
India, and Oscar has to look it up; about three cents and two Firecrawl
credits):

```bash
.venv/bin/python -m scripts.live_call_check --search
```

## How it works

```
Caller ──audio──▶ transport ──▶ turn detection ──▶ the caller's turn, as audio ─┐
   ▲                                                                            ▼
   └──────────audio◀── transport ◀── the answer, as audio ◀── speech-to-speech model
```

- **Browser calls** use WebRTC: the dashboard sends an offer to `POST /api/calls/browser`.
- **Phone calls** use Twilio Media Streams. Twilio posts to `/twilio/voice`
  (incoming) or Oscar calls Twilio's API (outgoing). Twilio then streams the
  call audio to the `/twilio/media` WebSocket.
- Both kinds of call run the same pipeline (`app/agent.py`).
- Hearing and speaking are one streamed request per turn to OpenRouter's
  `/chat/completions` (`app/speech.py`): the caller's audio goes in, the model's
  speech and its text come out. Earlier turns go along as text.
- The caller's words are transcribed separately (`/audio/transcriptions`), in
  the background. The dashboard shows that text, and it is also how Oscar
  remembers the caller: earlier turns go to the model as text, not as audio. So
  the transcriber is asked for several languages (`TRANSCRIPT_LANGUAGE` in
  `app/speech.py`). Asked for English alone, it wrote a caller's Hindi as
  nothing, and Oscar forgot what they had said two turns before.
- With web search set up, or a fact sheet filled in, a second request goes out
  alongside each turn: the same model hears the same audio and answers, in
  text, whether the turn needs a search or the fact sheet (`app/search.py`).
  Nothing of the spoken reply plays until that is known, which is usually
  before the reply is ready. If a search is needed, the reply is dropped, the
  caller hears a "one moment" line recorded in Oscar's voice (once, and kept in
  `data/recordings/`), Firecrawl searches, a text model writes a one or two sentence answer
  from what was found, and the speech model says that answer word for word. A
  question for the fact sheet goes the same way, without the search.
- What each search found is kept for the call (`WebLookup` in `app/search.py`).
  The text model that writes the answer is given the call so far, the earlier
  findings and the new results, and says first whether the results hold the
  answer. If they do not, it names one more thing to search for.
- The speech model is never handed the search results, a search tool or the
  fact sheet. All three were tried on real calls, and each cost it the agent's
  accent on a quarter to half of its replies.
- The accent is asked for in the instructions and again in a short note just
  before the caller's turn in every request (`ACCENT_NOTE` and
  `ACCENT_REMINDER` in `app/agent.py`). Asked for only once, it faded on some
  replies.
- On browser calls without the headphones option, the caller's audio is
  replaced with silence while Oscar speaks and for 0.3 seconds after
  (`EchoGate` in `app/speech.py`), so Oscar never hears its own voice.
- When the caller's words sound unfinished, Oscar waits a little under a second
  of silence before answering. If they carry on, or cut in during the first second of
  Oscar's answer, their earlier words are sent again along with the new ones.
- A request for an answer that fails before any of it has been said (the
  provider returns an error, or the connection drops) is made once more. A
  refusal that would only repeat, such as a rejected key or an empty account,
  is not, and an answer cut off part-way is not said over again.
- Connections to OpenRouter stay open between turns (`new_client` in
  `app/openrouter.py`) and are opened as each call starts, so a turn never
  waits for a new one. httpx's own default closed them after 5 idle seconds,
  which is less than a turn and its reply take.
- Each finished turn is saved to SQLite (`data/oscar.db`) and pushed to the
  dashboard live over Server-Sent Events (`/api/events`).
- With a script, a text model is asked after each reply which step Oscar is on
  (`app/script.py`), and the next step is put in the note the speech model
  reads before the caller's next turn, after the accent reminder.
- When a call ends, its transcript goes to a text model once, which writes the
  summary (`app/summary.py`). `POST /api/calls/{id}/summary` writes it again.
- Each web search's pages are saved with the call. When it ends, a second
  request gives a text model the transcript and those pages, and it writes the
  research notes (`app/research.py`). Its figures are then checked against the
  pages by Oscar itself, not by a model.

**Security:**

- Twilio requests are checked against Twilio's signature.
- Each audio stream must present a one-time token created for that call. The
  token stops working when the call ends, and a call whose audio has not
  connected within 3 minutes is marked failed.
- The dashboard has no password. With `HOST=127.0.0.1` (the default) only
  this computer can open it, and it only answers requests addressed to
  `localhost` or `127.0.0.1`. With `HOST=0.0.0.0`, anyone who can reach the
  port can use it, including your OpenRouter key, your call history and calls
  through Twilio, unless Cloudflare Access is set up (`app/access.py`, see
  "Protect the dashboard"). Then every request but `/healthz` and Twilio's
  webhooks needs the token Cloudflare signs for a visitor it let through:
  signed with one of the team's keys, made for this application and not
  expired. Oscar fetches the keys from Cloudflare, and fetches them again when
  Cloudflare changes them.
- Calls are capped at 30 minutes.

## Project layout

| Path | What it is |
|---|---|
| `app/agent.py` | The call pipeline: turn-taking around the speech model |
| `app/speech.py` | The speech-to-speech model, through OpenRouter |
| `app/search.py` | Web search through Firecrawl. For calls, also when to search or use the fact sheet, and writing the answer |
| `app/openrouter.py` | Shared OpenRouter helpers (error messages, headers) |
| `app/main.py` | Web server: dashboard, JSON API, browser calls |
| `app/phone.py` | Twilio: incoming/outgoing calls, signature checks, media stream |
| `app/script.py` | The owner's script: its steps, the note the speech model gets, and the model that keeps Oscar's place |
| `app/summary.py` | The note written when a call ends: summary, name, number to call back, what to do |
| `app/research.py` | The research notes written when a call ends: what its web searches found, checked against the pages |
| `app/calls.py` | Call bookkeeping and live updates |
| `app/store.py` | SQLite storage and agent settings |
| `app/config.py` | Settings from `.env` |
| `app/static/` | The dashboard (plain HTML, CSS, JavaScript) |
| `app/whatsapp/` | The WhatsApp process: `bridge.py` (connection), `chat.py` (when to answer, what Oscar reads), `brain.py` (asking the model) |
| `tests/` | Automated tests, including whole browser and phone calls |
| `tests_whatsapp/` | Tests for the WhatsApp connection (run with `.venv-whatsapp`) |
| `scripts/live_call_check.py` | One real call with your keys (`--search` to test web search) |
| `scripts/set_keys.py` | Saves your keys into `.env` without showing them (`--phone` for the Twilio details) |
| `scripts/autostart.py` | Has macOS start Oscar at login and keep it running (`--status`, `--restart`, `--remove`) |
| `deploy.sh` | Sets Oscar up on a Linux server, or brings it up to date, under pm2 (`--whatsapp`, `--force`) |
| `ecosystem.config.cjs` | How pm2 runs Oscar and its WhatsApp connection on a server |

## Troubleshooting

- **"Calls: key missing":** save your OpenRouter key with
  `.venv/bin/python -m scripts.set_keys`, then restart the server.
- **No microphone prompt / "cannot use the microphone":** browsers only allow
  the microphone on `http://localhost`, `http://127.0.0.1` or https pages.
- **The agent never answers, or stays silent:** the reason appears under the
  call button during the call and in **Call history** afterwards, for example
  a rejected OpenRouter key or an account with no credit. When the speech
  model's provider fails for a moment, Oscar asks once more by itself, so a
  reason shows only if the second try failed too.
- **"Web search: off":** save a Firecrawl key with
  `.venv/bin/python -m scripts.set_keys`, then restart the server.
- **Oscar says "Sorry, I couldn't look that up just now":** the search itself
  failed. The reason is in the call's transcript and in **Call history**, for
  example a rejected Firecrawl key or an account with no credits.
- **Oscar forgets what the caller said earlier in the call:** look at the
  call's transcript. Oscar remembers the caller's words as they are written
  there, so a line that is missing or wrong is a line it has forgotten. English
  and Hindi, mixed too, are written down; a language the transcriber does not
  take is not.
- **"Could not keep Oscar's place in its script":** the request that works out
  the step failed, for the reason shown (for example no OpenRouter credit).
  Oscar carries on from the last step it was known to be on, so it may ask
  something twice.
- **A call's summary says it could not be written:** open the call in **Call
  history**. The reason is shown there, for example an OpenRouter account with
  no credit, and **Write a summary** tries again.
- **A call's research notes say they could not be written:** the reason is
  shown under **Research** when you open the call. **Write the summary again**
  tries again. "Did not match the pages" means every note the model wrote gave
  a figure that was not on the pages, so none was kept.
- **Phone call fails right away:** check the Twilio number, that `PUBLIC_URL`
  matches the running tunnel, and (for trial accounts) that the number you
  dial is verified.
- **Browser call fails with "The call's audio could not reach Oscar's
  server":** the server's firewall does not let the call's audio in. Allow the
  UDP ports the message names, as in [Run it on a server](#run-it-on-a-server-pm2).
- **Browser call from another device over the tunnel doesn't connect:** browser
  call audio travels directly between the two devices, not through the tunnel,
  and many networks block that. Make browser calls on the computer running
  Oscar; phone calls work from anywhere.
- **WhatsApp: Oscar doesn't answer a mention:** check the number on the
  dashboard's WhatsApp card. If it is your own number, your phone scanned the
  code instead of Oscar's: Oscar is then signed in as you, so a mention of the
  Oscar account never reaches it. Stop the WhatsApp process (Ctrl+C), unlink
  your number, then start the process again and scan the new code with Oscar's
  phone:

  ```bash
  cd ~/Desktop/Oscar && .venv-whatsapp/bin/python -m app.whatsapp --unlink
  ```

  That signs Oscar out of the account and deletes the saved login. Removing
  **Oscar** under **WhatsApp → Settings → Linked devices** on the phone does
  the same. If the number is right, mention Oscar again: a message sent
  in the first minute after linking can miss the new connection. To see what
  arrives, start the process with `--verbose`. It logs each message's kind and
  whether it was for Oscar, never its words:

  ```bash
  cd ~/Desktop/Oscar && .venv-whatsapp/bin/python -m app.whatsapp --verbose
  ```
- **WhatsApp: "The linking code expired":** Oscar keeps a fresh code on the
  dashboard for an hour after it starts, then stops asking for new ones.
  Restart the WhatsApp process and scan the new code.
- **WhatsApp: "This connection library is out of date":** WhatsApp changed its
  protocol. Update the library, then restart the WhatsApp process:

  ```bash
  uv pip install --python .venv-whatsapp/bin/python --upgrade neonize protobuf
  ```

- **WhatsApp: Oscar doesn't answer in a group:** check the WhatsApp card. It
  should say **Connected**, and **Recent questions** shows errors such as an
  invalid OpenRouter key. Make sure the message has *@oscar* (or Oscar's
  current name), mentions Oscar from the @ list, or replies to Oscar.
- **WhatsApp: "failed to find libmagic":** run `brew install libmagic`.
