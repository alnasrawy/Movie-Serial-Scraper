# نشر المشروع على الإنترنت (VPS)

هذا الدليل يشرح كيفية رفع **Movie-Serial-Scraper** ليعمل كخادم دائم على
الإنترنت، بحيث يستدعيه تطبيق Alnasrawy TV عبر `POST /watch`.

> **مهم:** GitHub لا يشغّل الكود. الخادم هو جهاز (VPS) يشغّل `uvicorn` عبر
> Docker ويستقبل الطلبات 24/7.

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

- الحاوية تفتح Chromium رأس بلا واجهة؛ تأكد أن الذاكرة لا تقل عن 2 GB.
- إعدادات المواقع تُحفظ في مجلد `configs/` على الخادم (volume) وتبقى بعد
  إعادة البناء.
- أول استدعاء `/watch` قد يستغرق 30–90 ثانية (كشط + حلّ سيرفرات بمتصفح).
  التطبيق يجب أن يعرض مؤشر تحميل، وأن يعيد الاستدعاء عند انتهاء التوكينز.
