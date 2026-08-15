# Web Scraper for Movies & Series + Embed Resolver

منظومة Python متكاملة تبحث عن الأفلام والمسلسلات في مواقع (اكوام / ايجي ديد)،
تستخرج سيرفرات المشاهدة، تتحقق من الروابط، وتحوّل روابط الـ embed إلى
**بثّ HLS قابل للتشغيل في مشغّل ناتيف (ExoPlayer / VLC)** عبر متصفح رأس بلا واجهة.

---

## البنية

```
project/
├── cli.py                 # CLI الرئيسي: بحث + تصدير
├── final_links.py         # أمر واحد: يبحث ويحلّ ويطبع الروابط النهائية الجاهزة
├── requirements.txt       # تبعيات التشغيل
├── requirements-dev.txt   # تبعيات الاختبار (pytest, httpx)
├── configs/               # ملفات إعداد المواقع (JSON فقط — بدون كود)
│   ├── akwams.json
│   └── egydead.json
├── scraper/               # محرك الكشط
│   ├── base.py            #   العقدة المجردة (BaseScraper / SiteConfig)
│   ├── fetcher.py         #   طبقة HTTP: مهذّب + إعادة محاولة + budget
│   ├── generic.py         #   كشط عام بمحددات CSS من الإعداد
│   ├── sites.py           #   تسجيل المواقع وبناء الكاشطات
│   ├── resolver.py        #   فك روابط embed إلى روابط مباشرة (regex/packer)
│   ├── verify.py          #   فحص الروابط الحية + ترقيم السيرفرات
│   ├── storage.py         #   تصدير JSON/CSV
│   └── tmdb.py            #   تحويل TMDB id إلى عنوان بحث
├── middleware/            # الوسيط: يحوّل embed → بث HLS
│   ├── server.py          #   FastAPI: /resolve /stream /health
│   └── player.py          #   جلسات Chromium + جلب عبر ctx.request + تجديد التوكين
└── tests/                 # 85 اختبارًا (بدون اتصال خارجي)
```

## التثبيت

```bash
pip install -r requirements.txt
python -m playwright install chromium     # مطلوب للوسيط فقط
```

للاختبارات:
```bash
pip install -r requirements-dev.txt
```

---

## CLI — البحث والتصدير

```bash
# عرض المواقع المضبوطة
python cli.py --list

# بحث باسم
python cli.py --query "Spider-Man: Brand New Day" --sites "akwams,egydead"

# مشاهدة فقط (بدون روابط التحميل)
python cli.py --query "inception" --sites "akwams,egydead" --watch-only

# بحث برقم TMDB (المفتاح = 32 حرفًا من themoviedb.org/settings/api)
python cli.py --tmdb 27205 --type movie --tmdb-key "YOUR_32_CHAR_KEY"
# أو عبر متغير بيئة:  $env:TMDB_API_KEY="..."

# تصدير CSV/JSON لملف محدد
python cli.py --query "godfather" --format csv --out result.csv

# خيارات الأداء والفحص
python cli.py --query "inception" --no-verify --delay 0.5 --max-pages 2 -v
```

### خيارات CLI

| الخيار | الوظيفة |
|---|---|
| `--query "..."` | نص البحث (اقتبسه إذا فيه مسافات) |
| `--tmdb <id>` / `--tmdb-key` | بحث برقم TMDB |
| `--type movie\|tv` | نوع العمل مع TMDB |
| `--sites "akwams,egydead"` | المواقع المطلوبة |
| `--watch-only` | سيرفرات المشاهدة فقط، بدون تحميل |
| `--no-details` | بدون صفحات التفاصيل (أسرع) |
| `--no-resolve` | بدون محاولة فك الروابط المباشرة |
| `--no-verify` | بدون فحص الروابط الحية |
| `--no-label` | بدون إعادة التسمية (سيرفر 1..) |
| `--format json\|csv` / `--out` | صيغة ومسار الحفظ |
| `--delay` / `--jitter` / `--max-pages` / `--timeout` | أداء/مهذّب |
| `-v` | طباعة النتيجة كاملة |

---

## final_links.py — الروابط النهائية بأمر واحد

بحث + حلّ كل سيرفر في المتصفح + وسيط بثّ حي، فيطلب منك فقط **رابطًا جاهزًا**:

```bash
python final_links.py "Spider-Man: Brand New Day"
python final_links.py "inception" --sites akwams --out final.json
```

ما يحدث:
1. يبحث في المواقع (مشاهدة فقط).
2. يفتح كل سيرفر في Chromium رأس بلا واجهة ويلتقط الفيديو.
3. يطبع الروابط النهائية: `http://127.0.0.1:8000/stream?...` — الصقها في VLC/ExoPlayer.
4. يبقى الوسيط شغالًا حتى تضغط `Ctrl+C` (لأن توكين الفيديو ينتهي بعد دقائق).

خيارات: `--sites` ، `--max-servers` ، `--timeout` ، `--out` ، `--port`.

---

## الوسيط (Middleware)

لوحدك — تخدم الروابط عبر HTTP:

```bash
python -m middleware            # uvicorn على 127.0.0.1:8000
```

| النقطة | الطريقة | الوصف |
|---|---|---|
| `POST /watch` | JSON `{"tmdb_id": 27205, "type": "movie", "sites": [...]}` | **العقدة الجوهرية**: TMDB id → قائمة سيرفرات جاهزة (`proxy_url`) |
| `POST /resolve` | JSON `{"url": embed, "referer": اختياري}` | يحلّ الـ embed → `sid`, `kind`, `proxy_url` |
| `GET /stream?sid=&url=` | — | يعيد كتابة قوائم HLS ويمرر المقاطع عبر جلسة المتصفح |
| `DELETE /session/{sid}` | — | إغلاق جلسة |
| `GET /health` | — | الحالة |

### تدفق التشغيل من التطبيق

```
التطبيق يعرض معلومات TMDB → المستخدم يضغط "مشاهدة"
  → التطبيق يرسل POST /watch  {"tmdb_id": ...}
  → الخادم: يبحث بالعنوان العربي → يكشط المواقع → يحلّ السيرفرات بالمتصفح
  → يرد قائمة: [ {name: "سيرفر 1", proxy_url: ".../stream?..."} ... ]
  → التطبيق يعرض القائمة → عند الاختيار يغذّي proxy_url لمشغل ExoPlayer
```

## النشر على الإنترنت

هذا المستودع كود فقط — GitHub لا يشغّله. لنشر الخادم ليعمل 24/7 عند
الاستدعاء من تطبيقك، اتبع **[DEPLOYMENT.md](DEPLOYMENT.md)** (Docker + VPS،
دقائق قليلة): `docker compose up -d --build` ثم ضع `http://IP:8000` في تطبيقك.

### لِمَ هذا مطلوب؟
- مضيفو الفيديو (vibuxer/hgcloud/EarnVids...) يولّدون التوكين بالجافاسكربت
  ويتحققون من الـ Referer — روابط embed **لا تعمل في مشغّل ناتيف مباشرة**.
- `yt-dlp` لا يدعم هذه المضيفات.
- الوسيط يفتح الـ embed في متصفح حقيقي، يلتقط قائمة HLS، يعيد كتابتها لتمر عبره،
  ويجدد التوكين عند انتهائه (وهو ما يبقي التشغيل مستمرًا خلال فيلم كامل).

---

## إضافة موقع جديد

ملف JSON فقط في `configs/`:

```json
{
  "name": "mysite",
  "base_url": "https://www.mysite.com/",
  "search_url": "https://www.mysite.com/search/{query}",
  "item_selector": "article.movie-card",
  "fields": {
    "title": "h2",
    "year": ".year",
    "detail_url": "a@href"
  },
  "custom": {
    "detail_fields": { "description": ".synopsis" },
    "watch_servers": {
      "item_selector": "ul.serversList > li[data-link]",
      "fields": { "name": "p", "url": "@data-link" }
    },
    "extra_detail_pages": [
      { "suffix": "watch", "servers": ["watch_servers"] }
    ]
  }
}
```

- صيغة الحقول: `h2` (نص)، `a@href` (صفة من عنصر فرعي)، `@data-link` (صفة من العنصر نفسه).
- مواقع تحتاج POST (مثل ايجي ديد): أضف `"detail_method": "post"` و`"detail_data": {"View": "1"}`.
- تفعيل الفك/الفحص/الترقيم: `"resolve_servers": true` ، `"verify_servers": true` ، `"label_servers": true`.

---

## الاختبارات

```bash
python -m pytest tests -q
```

38 اختبارًا تغطي: محرك الكشط العام، فك الحزم، الفحص، الترقيم، التخزين، TMDB،
دوال الوسيط ومسارات HTTP (بمحاكاة بدون متصفح)، وسيناريو CLI كامل ضد موقع تجريبي محلي.

## ملاحظة قانونية

اسحب البيانات من مواقع تملكها أو لديك إذن منها فقط، واحترم `robots.txt`
وسياسة الاستخدام لكل موقع.
