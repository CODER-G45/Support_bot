# Support Bot v2 – Render Deployment

## 1. GitHub pe daalo
Ye files repo me push karo: `bot.py`, `requirements.txt`, `render.yaml`, `.gitignore`
(`.env` kabhi push mat karna).

## 2. Render pe deploy
1. render.com -> **New +** -> **Web Service** -> apna repo select karo
   (ya **Blueprint** choose karo, `render.yaml` auto-detect ho jayega).
2. Agar manual: Runtime **Python 3**, Build `pip install -r requirements.txt`,
   Start `python bot.py`, Plan **Free**.
3. **Environment** me daalo:
   - `BOT_TOKEN` = BotFather wala token
   - `ADMIN_ID`  = tumhara numeric Telegram ID (@userinfobot se)
   - `PYTHON_VERSION` = `3.11.9`
4. Deploy. Logs me `Support bot is up.` dikhna chahiye.

## 3. UptimeRobot (15 min sleep rokne ke liye)
1. uptimerobot.com pe free account banao -> **Add New Monitor**
2. Type: **HTTP(s)**, URL: `https://<tumhara-app>.onrender.com/health`
3. Interval: **5 minutes** -> Create.

Render free service 15 min bina traffic ke so jati hai; UptimeRobot har 5 min
ping karta hai to ye kabhi sleep nahi hoti. Backup ke taur par bot khud ko bhi
har 10 min ping karta hai.

## 4. Data safety (IMPORTANT)
Render free plan ki disk **ephemeral** hai – redeploy/restart par
`support_data.json` mit sakta hai. Isliye:
- Bot har 12 ghante (agar data badla ho) tumhe backup file Telegram pe bhejta hai.
- Kabhi bhi `/backup` se manual backup lo.
- Redeploy ke baad wahi `.json` file caption `/restore` ke sath bot ko bhejo.

Permanent storage chahiye to Render paid disk lo aur `DATA_DIR=/data` set karo.

## Admin commands
`/open` `/chat <id>` `/find` `/users` `/stats` `/ban` `/unban`
`/broadcast` `/setwelcome` `/backup` – details ke liye `/help`.
