# Movie / Series Scraper + Embed Resolver

منظومة Python تبحث عن الأفلام والمسلسلات في مواقع (اكوام / لاروزا)، تستخرج
سيرفرات المشاهدة، وتحوّل روابط الـ embed إلى **بثّ HLS قابل للتشغيل في مشغّل
ناتيف (ExoPlayer / VLC)**.

---

## البنية

مصدر الكود الوحيد هو `film_scraper/` (قابلة للنشر مستقلة):

```
film_scraper/
├── run.py                 # نقطة الدخول: --list --serve --query --final --direct
├── requirements.txt       # تبعيات التشغيل
├── configs/               # ملفات إعداد المواقع (JSON فقط — بدون كود)
│   ├── akwams.json
│   ├── larroza.json
│   └── providers.json     # إعدادات الوسطاء (الترجمة OpenSubtitles...)
├── scraper/               # محرك الكشط
│   ├── base.py            #   العقدة المجردة (BaseScraper / SiteConfig)
│   ├── fetcher.py         #   طبقة HTTP: مهذّب + إعادة محاولة + budget
│   ├── generic.py         #   كشط عام بمحددات CSS من الإعداد
│   ├── sites.py           #   تسجيل المواقع وبناء الكاشطات
│   ├── resolver.py        #   فك روابط embed إلى روابط مباشرة (regex/packer)
│   ├── verify.py          #   فحص الروابط الحية + ترقيم السيرفرات
│   ├── storage.py         #   تصدير JSON/CSV
│   └── tmdb.py            #   تحويل TMDB id إلى عنوان بحث
└── middleware/            # الوسيط: يحوّل embed → بث HLS
    ├── server.py          #   FastAPI: /watch /resolve /stream /subtitle /health
    ├── http_resolver.py   #   حلّ HTTP خالص (بدون متصفح) لمضيفات EarnVids
    ├── player.py          #   جلسات Chromium + جلب عبر ctx.request + تجديد التوكين
    ├── subtitles.py       #   OpenSubtitles → WebVTT
    └── envfile.py         #   محمّل .env بدون تبعيات
```

## التثبيت

```bash
cd film_scraper
pip install -r requirements.txt
python -m playwright install chromium     # مطلوب للوسيط فقط (BROWSER_ENABLED=1)
```

للاختبارات (من جذر المشروع):
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

---

## CLI — البحث والتصدير

```bash
# عرض المواقع المضبوطة
python run.py --list

# بحث باسم
python run.py --query "inception" --sites "akwams,larroza"

# مشاهدة فقط (بدون روابط التحميل)
python run.py --query "inception" --sites "akwams,larroza" --watch-only

# بحث برقم TMDB (المفتاح في .env: TMDB_API_KEY=...)
python run.py --tmdb 27205 --type movie

# روابط مباشرة (بدون بروكسي)
python run.py --direct "inception"

# تصدير CSV/JSON لملف محدد
python run.py --query "godfather" --format csv --out result.csv
```

---

## الوسيط (Middleware)

```bash
python run.py --serve          # uvicorn على 0.0.0.0:8000
python -m middleware           # أو مباشرة
```

| النقطة | الطريقة | الوصف |
|---|---|---|
| `POST /watch` | JSON `{"tmdb_id": 27205, "type": "movie", "sites": [...]}` | **العقدة الجوهرية**: TMDB id → قائمة سيرفرات جاهزة (`proxy_url`) |
| `POST /resolve` | JSON `{"url": embed, "referer": اختياري}` | يحلّ الـ embed → `sid`, `kind`, `proxy_url` |
| `POST /direct` | — | روابط CDN المباشرة (m3u8) بدون بروكسي |
| `GET /stream?sid=&url=` | — | يعيد كتابة قوائم HLS/MPD ويمرر المقاطع عبر جلسة المتصفح |
| `GET /subtitle?imdb_id=&lang=` | — | ترجمة WebVTT من OpenSubtitles |
| `GET /tmdb/popular|trending|search` | — | تصفح TMDB لتطبيق أندرويد |
| `GET /health` | — | الحالة |

### تدفق التشغيل من التطبيق

```
التطبيق يعرض معلومات TMDB → المستخدم يضغط "مشاهدة"
  → التطبيق يرسل POST /watch  {"tmdb_id": ...}
  → الخادم: يبحث بالعنوان → يكشط المواقع → يحلّ السيرفرات (HTTP أو متصفح)
  → يرد قائمة: [ {name: "سيرفر 1", proxy_url: ".../stream?..."} ... ]
  → التطبيق يعرض القائمة → عند الاختيار يغذّي proxy_url لمشغل ExoPlayer
```

## النشر على الإنترنت

اتبع **[DEPLOYMENT.md](DEPLOYMENT.md)** (Docker + VPS أو Render):
`docker compose up -d --build` ثم ضع `http://IP:8000` في تطبيقك.
`Dockerfile.lite` = حلّ HTTP خالص للخطط المجانية (بدون متصفح).

### لِمَ الوسيط مطلوب؟
- مضيفو الفيديو (vibuxer/hgcloud/EarnVids...) يولّدون التوكين بالجافاسكربت
  ويتحققون من الـ Referer — روابط embed **لا تعمل في مشغّل ناتيف مباشرة**.
- الوسيط يفتح الـ embed في متصفح حقيقي (أو يحلّه HTTP للمضيفات الداعمة)، يلتقط
  قائمة HLS، يعيد كتابتها لتمر عبره، ويجدد التوكين عند انتهائه.

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

- `"enabled": false` يعطّل الموقع دون حذفه.
- صيغة الحقول: `h2` (نص)، `a@href` (صفة من عنصر فرعي)، `@data-link` (صفة من العنصر نفسه).
- مواقع تحتاج POST (مثل ايجي ديد): أضف `"detail_method": "post"` و`"detail_data": {"View": "1"}`.

## الاختبارات

```bash
python -m pytest tests -q
```

74 اختبارًا تغطي: محرك الكشط العام، فك الحزم، الفحص، الترقيم، التخزين، TMDB،
دوال الوسيط ومسارات HTTP (بمحاكاة بدون متصفح)، وسيناريو CLI كامل ضد موقع تجريبي محلي.

## ملاحظة قانونية

اسحب البيانات من مواقع تملكها أو لديك إذن منها فقط، واحترم `robots.txt`
وسياسة الاستخدام لكل موقع.