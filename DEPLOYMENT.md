# نشر المشروع على الإنترنت

هذا الدليل يشرح كيفية رفع **Movie-Serial-Scraper** ليعمل كخادم دائم على
الإنترنت، بحيث يستدعيه تطبيق Alnasrawy TV عبر `POST /watch`.

> **مهم:** GitHub لا يشغّل الكود. الخادم هو جهاز (VPS) أو خدمة PaaS (Render)
> يشغّل `uvicorn` عبر Docker ويستقبل الطلبات 24/7.

---

## 0) تجربة سريعة على Render (مجاني)

1. سجّل على https://dashboard.render.com (الدخول بحساب GitHub).
2. من `New +` اختر **Blueprint**.
3. اختر مستودع `Movie-Serial-Scraper` (سيجد `render.yaml` تلقائيًا).
4. ستظهر خدمة `movie-serial-scraper` — افتحها وضع **TMDB_API_KEY** من تبويب
   **Environment** (ثم Save Changes).
5. اضغط **Apply** ثم انتظر البناء (5–10 دقائق أول مرة).
6. عند اكتماله ستحصل على رابط مثل:
   ```
   https://movie-serial-scraper.onrender.com
   ```
7. جرّب من جهازك:
   ```
   https://movie-serial-scraper.onrender.com/health
   ```
8. وضع الرابط كعنوان الخادم في تطبيق Alnasrawy TV.

> **تنبيهات التجربة المجانية:** الخدمة تنام بعد ~15 دقيقة خمول (أول استدعاء
> بعده يستغرق ~دقيقة).

> **مهم (المعمارية):** الخطة المجانية تُنشر بصورتين:
> - **Dockerfile.lite** (الافتراضي على Render): محلل **HTTP نقي** — بلا متصفح —
>   يغلّف سيرفرات عائلة EarnVids (smoothpre) التي توفّرها akwams/egydead.
>   يشتغل بثبات على 512MB.
> - **Dockerfile** (لـ VPS / خطة مدفوعة): مع Playwright/Chromium و`BROWSER_ENABLED=1`
>   لحلّ مضيفي vibuxer/hgcloud الذين يلزمهم JS. يحتاج ≥ 1GB ذاكرة.
>   لتفعيله غيّر `render.yaml` (أو استخدم `docker compose` على VPS).

---

## 1) اختر خادمًا (VPS) وأنشئه

موصى به: Ubuntu 22.04، 2 GB RAM كحد أدنى، وحدة معالجة واحدة على الأقل.

- Hetzner: https://www.hetzner.com/cloud  (~4 دولار/شهر)
- Contabo: https://contabo.com
- DigitalOcean: https://digitalocean.com
- استضافة عربية محلية حسب موقعك

بعد إنشائه ستحصل على: **IP الخادم** + مستخدم `root` + كلمة مرور/مفتاح SSH.

## 2) ادخل على الخادم (SSH)

من جهازك (PowerShell أو Terminal):
```bash
ssh root@IP_ADDRESS
```

## 3) ثبّت Docker على الخادم

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

## 4) انسخ المشروع على الخادم

```bash
git clone https://github.com/alnasrawy/Movie-Serial-Scraper.git
cd Movie-Serial-Scraper
cp .env.example .env
```

ثم افتح `.env` وضع مفتاح TMDB الصحيح (32 حرفًا):
```bash
nano .env        # عدّل TMDB_API_KEY=... ثم Ctrl+X ثم Y ثم Enter
```

## 5) شغّل الخادم

```bash
docker compose up -d --build
```

أول تشغيل يستغرق عدة دقائق (تحميل Python + Chromium داخل الحاوية).

تحقق من عملها:
```bash
curl http://127.0.0.1:8000/health
# -> {"ok":true,"sessions":0}
```

مشاهدة السجلات:
```bash
docker compose logs -f api
```

## 6) افتح المنفذ في جدار الحماية

على الخادم:
```bash
ufw allow 8000
ufw enable
```
(أو افتح منفذ 8000 من لوحة تحكم مزودك)

اختبر من جهازك:
```
http://IP_ADDRESS:8000/health
```

## 7) الربط مع التطبيق

في تطبيق Alnasrawy TV ضع عنوان الخادم:
```
http://IP_ADDRESS:8000
```

تجربة النقطة الجوهرية:
```bash
curl -X POST http://IP_ADDRESS:8000/watch \
  -H "Content-Type: application/json" \
  -d '{"tmdb_id": 27205, "type": "movie"}'
```

يجب أن يعيد قائمة سيرفرات جاهزة (`proxy_url` لكل سيرفر). أرسل أي `proxy_url`
إلى مشغل ExoPlayer مباشرة.

## 8) (اختياري) دومين + HTTPS

مع عنوان IP فقط يعمل HTTP. للـ HTTPS تحتاج دومين وNginx:
```bash
apt install nginx certbot python3-certbot-nginx
```
ثم ملف `/etc/nginx/sites-available/movie`:
```
server {
    listen 80;
    server_name api.example.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```
```bash
ln -s /etc/nginx/sites-available/movie /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d api.example.com
```

## التحديث بعد تغيير الكود

```bash
cd Movie-Serial-Scraper
git pull
docker compose up -d --build
```

## ملاحظات تقنية للنشر

- الحاوية الكاملة تفتح Chromium رأس بلا واجهة؛ تأكد أن الذاكرة لا تقل عن 2 GB.
  الحاوية الخفيفة (lite) تحتاج فقط ~100MB.
- إعدادات المواقع تُحفظ في مجلد `configs/` على الخادم (volume) وتبقى بعد
  إعادة البناء.
- أول استدعاء `/watch` قد يستغرق 20–60 ثانية (كشط + حلّ سيرفرات HTTP). على
  الخطة الخفيفة تُحلّ سيرفرات smoothpre فقط، وتُتخطّى بقية المضيفين.
  التطبيق يجب أن يعرض مؤشر تحميل، وأن يعيد الاستدعاء عند انتهاء التوكينز.
- لتفعيل مسار المتصفح محليًا أو على VPS:
  ```bash
  BROWSER_ENABLED=1 python -m middleware
  ```
