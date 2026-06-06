# V_2rubby — ربات شخصی روبیکا با پنل تلگرام + سیستم Worker

یک ربات شخصی روبیکا که از طریق پنل تلگرام کنترل می‌شه. پیام نشان‌دارِ Saved Messages
رو به مخاطب‌های خودت forward می‌کنه. حالا با یک **سیستم توزیع‌شده‌ی Master/Worker**
که بار لاگین و ارسال رو بین چند سرور (چند IP) پخش می‌کنه.

> منطق ارسال/لاگین (`rubika_client.py`) دست‌نخورده‌ست؛ ورکرها **همون کد** رو اجرا می‌کنن.

---

## 🧠 معماری

- **Master:** همون جایی که ربات تلگرام بالاست. فرمان می‌ده، وضعیت ورکرها رو می‌گیره و لاگ می‌زنه.
- **Worker:** سرورهایی که از داخل پنل اضافه می‌کنی. مستر با SSH واردشون می‌شه، Docker نصب می‌کنه،
  سورس رو می‌آره و یک نود API بالا میاره (`MODE=worker`).
- **ارتباط:** از داخل **تونل SSH** (هیچ پورتی روی ورکر در معرض اینترنت نیست).
- **پخش:** لاگین جدید به‌صورت round-robin بین ورکرهای سالم تقسیم می‌شه. اگر یکی خراب بود،
  خودکار می‌ره سراغ بعدی. هر اکانت بعد از لاگین به **همون ورکر** گره می‌خوره و ارسالش هم از همون‌جاست.

---

## ✅ پیش‌نیازها

**سرور Master:** لینوکس (اوبونتو پیشنهاد می‌شه)، Python 3.10+، گیت.
**سرور Worker:** اوبونتو/دبیان با دسترسی **root** و یک یوزر/پسورد SSH. (Docker رو خود مستر نصب می‌کنه.)

---

## ⚡ راه‌اندازی سریع (یک دستور)

ساده‌ترین راه؛ اسکریپت همه‌کار رو خودش انجام می‌ده (نصب، venv، کلید رمزنگاری،
گرفتن تنظیمات، و ساخت سرویس `rubika-master`):

```bash
git clone https://github.com/Shantae86525/Rubika_Runway
cd V_2rubby
git checkout feature/worker-admin-system
chmod +x setup.sh
./setup.sh
```

اسکریپت ازت `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `LOG_GROUP_ID` رو
می‌پرسه و خودش `WORKER_SECRET` رو می‌سازه. اگه با `root` اجراش کنی، می‌تونه
سرویس systemd به اسم `rubika-master` رو هم نصب کنه که بعد ری‌استارت سرور بالا بمونه.

```bash
journalctl -u rubika-master -f      # دیدن لاگ زنده
systemctl restart rubika-master     # ری‌استارت
systemctl stop rubika-master        # توقف
```

---

## 🚀 راه‌اندازی Master (دستی)

```bash
# ۱) گرفتن سورس
git clone https://github.com/Shantae86525/Rubika_Runway
cd V_2rubby

# ۲) محیط مجازی و نصب کتابخونه‌ها
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# ۳) ساخت فایل تنظیمات
cp .env.example .env
nano .env        # مقادیر رو پر کن (پایین توضیح داده شده)

# ۴) ساخت کلید رمزنگاری ورکرها و گذاشتنش در .env روی خط WORKER_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ۵) اجرا
python main.py
```

مقادیر ضروری `.env`:
- `API_ID` و `API_HASH` از https://my.telegram.org
- `BOT_TOKEN` از @BotFather
- `OWNER_ID` آیدی عددی خودت (از @userinfobot)
- `LOG_GROUP_ID` آیدی گروه لاگ (اول ربات رو ادمین اون گروه کن)
- `WORKER_SECRET` کلیدی که در مرحله‌ی ۴ ساختی

برای اینکه ربات بعد از بستن ترمینال هم بالا بمونه، می‌تونی با `systemd` یا
`screen`/`tmux` اجراش کنی. نمونه‌ی سرویس systemd پایین آمده.

---

## 🛠 افزودن Worker (از داخل تلگرام)

1. تو ربات `/start` بزن → **🛠 مدیریت ورکر** → **➕ افزودن ورکر**.
2. به ترتیب می‌فرستی: **IP سرور → پورت SSH (پیش‌فرض 22) → یوزرنیم → پسورد**.
3. مستر خودکار: SSH می‌زنه، Docker نصب می‌کنه، سورس رو clone و build می‌کنه،
   کانتینر ورکر رو با `--restart always` بالا میاره، و یک بار سلامتش رو چک می‌کنه.
4. لاگ `🛠 ADDED WORKER` و `🛠 STATU WORKER ALL` به گروه لاگ میاد.

از همون منو می‌تونی هر ورکر رو **قطع/وصل، ری‌استارت، آپدیت، حذف** کنی و **وضعیت/پینگ** ببینی.

> توجه: اضافه‌کردن ورکر نیاز به `WORKER_SECRET` در `.env` داره (برای رمزنگاری پسورد SSH).

---

## 📌 امکانات پنل

- ➕ افزودن اکانت (شماره → کد → رمز دومرحله‌ای) — خودکار به یک ورکر سالم وصل می‌شه
- 🚀 ارسال (forward پیام نشان‌دار) — از ورکرِ صاحب اکانت
- 📌 تنظیم مارکر (بدون نیاز به ویرایش `.env`)
- ⚙️ تنظیم سرعت ارسال (۰.۲ تا ۱۰ ثانیه)
- 👥 مدیریت ادمین (فقط مالک) — ادمین‌ها هم می‌تونن با ربات کار کنن
- 💾 بکاپ کامل (zip شامل دیتابیس + سشن همه‌ی اکانت‌ها از مستر و همه‌ی ورکرها)
- 🛠 مدیریت ورکر + گزارش سلامت هر ۳۰ دقیقه + هشدار فوری روی بلاک‌شدن

---

## 🐳 اجرای دستیِ یک Worker با Docker (اختیاری)

معمولاً لازم نیست (مستر خودکار انجام می‌ده)، ولی اگه خواستی دستی:

```bash
# روی سرور ورکر، داخل .env بذار:
#   MODE=worker
#   WORKER_API_TOKEN=<یک توکن دلخواه>
#   WORKER_API_PORT=8765
docker compose up -d --build
```

---

## 🧩 نمونه سرویس systemd برای Master

```ini
# /etc/systemd/system/v2rubby.service
[Unit]
Description=V2Rubby Master
After=network-online.target

[Service]
WorkingDirectory=/root/V_2rubby
ExecStart=/root/V_2rubby/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now v2rubby
sudo journalctl -u v2rubby -f      # دیدن لاگ‌ها
```

---

## ⚠️ نکته‌ی مهم (محدودیت ذاتی روبیکا)

سشن هر اکانت به همون ورکری که روش لاگین شده گره می‌خوره. اگر یک ورکر بیفته،
اکانت‌های همون ورکر تا برگشتنش منتظر می‌مونن؛ ولی بقیه‌ی اکانت‌ها روی ورکرهای دیگه
عادی کار می‌کنن و کل سیستم متوقف نمی‌شه.

---

## آپدیت

```bash
cd V_2rubby && git pull && pip install -r requirements.txt
sudo systemctl restart v2rubby     # اگه با systemd اجرا کردی
```
ورکرها رو هم از منوی **🛠 مدیریت ورکر → ⬆️ آپدیت** به‌روز کن.
