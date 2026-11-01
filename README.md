# المحاسبة المشتركة — Partnership Accounts

لوحة تحكم محاسبية مشتركة للشركاء والمحاسب، بالعربية والإنجليزية. كل شريك يرى كل حركة لحظة تسجيلها،
ولا يُحذف شيء دخل الحسابات أبداً.

A shared accounting dashboard for the partners and their accountant, in Arabic and English.
Every partner sees every transaction as soon as it is recorded, and nothing that reached the books is ever deleted.

[English below](#english)

---

## ماذا يفعل النظام

| الحركة | ماذا تفعل في الحسابات |
|---|---|
| الأرصدة الافتتاحية | ما تملكه الشركة يوم البدء (صناديق، مستحقات، أصول مثل البركس) ومن موّله. يجب أن يتساوى الطرفان حتى آخر أغورة. |
| طلب تمويل من الشركاء | يقسم المبلغ المطلوب على الشركاء حسب نسبهم (مثلاً بركس 180,000: المقاول 90,000 وكل شريك 30,000). للمتابعة فقط. |
| دفعة رأس مال من شريك | مال دفعه شريك إلى صندوق الشركة؛ يزيد رأس ماله. يمكن ربطه بطلب التمويل ليظهر «المتبقي عليه». |
| تسوية بين شريكين | شريك دفع لشريك آخر مباشرةً (مثل نصف تكلفة البركس للمقاول). |
| مصروف | لحم، رز، حطب، أجور، إيجار… من صندوق الشركة، أو دفعه شريك من ماله فيُضاف لرأس ماله. مع صورة الفاتورة. |
| شراء / بيع أصل | البركس والمعدات أصول لا مصاريف. عند البيع يُحسب الربح أو الخسارة تلقائياً. |
| مطالبة للمؤسسة | الكمية × سعر الوحدة (مثلاً 1000 وجبة × 17). تبقى ديناً على المؤسسة حتى تُستلم. |
| استلام دفعة | تُخصم من دين المؤسسة؛ عمولة التسييل تُسجَّل مصروفاً. |
| تحويل بين حسابين | صندوق ↔ بنك ↔ محفظة، مع العمولة. |
| توزيع أرباح | يُقسم على الشركاء حسب نسبهم. لا يُسمح بتوزيع ربح لم يُحصَّل من المؤسسة. |
| إنتاج يومي | عدد الوجبات أو الطرود، لحساب تكلفة الوجبة وربحها. |

### التقارير
حسابات الشركاء (رأس المال، حصة الربح، ما استلمه، صافي حقه، المتبقي عليه) · أرباح كل مشروع مع تكلفة وربح الوحدة
· ميزان المراجعة والمركز المالي · كشف كل صندوق بالرصيد الجاري · كشف المؤسسة · سجل الأصول · تصدير Excel.

## قواعد الدقة

**الأرقام.** يُقبل الإدخال بالأرقام العربية (١٢٣) والإنجليزية (123) والفارسية، وحتى المختلطة. الفاصلة العشرية `.` أو `٫`،
وفاصل الآلاف `,` أو `٬` أو `،` أو مسافة، بشرط أن تكون المجموعات ثلاثية صحيحة. لذلك يُرفض `12,5` بدل أن يُقرأ 125،
ويظهر للمستخدم: «استخدم النقطة للكسور». وكذلك يُرفض `1.500` بدل أن يُقرأ شيكلاً ونصفاً، مع سؤال: «هل تقصد 1500؟».
لا تقريب صامت أبداً: الشيكل خانتان عشريتان فقط. تُحذف علامات الاتجاه
المخفية التي تأتي مع النسخ واللصق، ويُقبل `₪` و«شيكل». تحت الخانة يظهر فوراً ما سيُحفظ فعلاً: «سيُحفظ: 1,234.50».
كل المبالغ تُخزَّن أعداداً صحيحة بالأغورة، فلا يوجد أي خطأ تقريب.

**النسب.** الأدق: «نسبة بأعداد صحيحة» 3:1:1:1، فيأخذ كل شريك الثلث بالضبط من الخمسين بالمئة. أي مبلغ يُقسم بطريقة
«الباقي الأكبر» فيكون مجموع الحصص مساوياً للمبلغ حتى الأغورة. للنسب تاريخ سريان، وتغييرها لا يغيّر أرباح الأيام السابقة. الربح يُقسم مرة واحدة لكل فترة نسب،
فلا يتراكم فرق التقريب يوماً بعد يوم.
عند توزيع الأرباح يأخذ كل شريك من ربحه الذي لم يستلمه بعد. ما دامت النسب لم تتغير فهذا يطابق نسب الحصص.
إن تغيرت، يأخذ كل شريك ما كسبه فعلاً بالنسب القديمة، ولا يستلم أي شريك أكثر من حصته من الربح.

**الحذف.**
- الحركة المرحَّلة **لا تُحذف أبداً**. تُلغى مع سبب مكتوب: يزول أثرها من كل الأرصدة، وتبقى ظاهرة مشطوبة، ويُسجَّل من ألغاها ومتى ولماذا.
- يُرفض الإلغاء إن كان سيُفسد الحسابات، مع رسالة تشرح السبب، مثلاً:
  - إلغاء دفعة شريك صُرف مالها ← «الرصيد لا يكفي: الحساب سيصبح سالباً (-4,000.00) بتاريخ 2026-10-03».
  - إلغاء مطالبة استُلم مبلغها، أو استلام وُزّع ربحه، أو شراء أصل بِيع، أو طلب تمويل عليه دفعات.
- الحذف النهائي فقط لحركة لم تدخل الحسابات (بانتظار الموافقة أو مرفوضة)، ويُسجَّل في سجل الرقابة مع نسخة كاملة منها.
- المشروع أو الحساب أو الجهة: يُحذف فقط إن لم يُستخدم أبداً، وإلا يُوقف (لا يظهر في الحركات الجديدة ويبقى في التقارير).
  لا يمكن إيقاف حساب رصيده ليس صفراً.
- المستخدم الذي له سجل يُوقف ولا يُحذف. لا يمكن إيقاف آخر مدير.

**الأرصدة الافتتاحية.** يجب إدخالها قبل أي حركة (ولو أصفاراً) لتحديد تاريخ البداية. لا تُقبل حركة بتاريخ أقدم منها.
تُعدَّل ما دام لم يُقفل أي شهر، ولا يُقبل تعديل يجعل رصيداً لاحقاً سالباً.

**ضوابط دائمة.** بعد كل عملية يتحقق النظام من أن:
- المدين يساوي الدائن.
- الأصول تساوي حقوق الشركاء.
- لا يصبح صندوق أو دين المؤسسة أو رأس مال شريك أو أصل سالباً في **أي يوم** (حتى مع التواريخ القديمة).
- لا يتجاوز التوزيع الربح المحصَّل.

إن فشل أي شرط تُرفض العملية كاملة ولا يبقى منها شيء.

**الموافقة الجماعية.** مصروف أو شراء أصل فوق الحد (من الإعدادات) لا يدخل الحسابات حتى يوافق كل شريك له حساب.
رفض واحد يرفضه، وأي تعديل يعيد جولة الموافقة.

**إقفال الشهر.** كل شريك يعتمد كشف الشهر، ثم يقفله المدير. الأشهر تُقفل بالترتيب وتُفتح من الأحدث، فلا يمكن لأي تغيير
بتاريخ قديم أن يغيّر رصيد شهر مقفل. بعد الإقفال لا إضافة ولا تعديل ولا إلغاء في ذلك الشهر.
أي تغيير قبل الإقفال يلغي الاعتمادات تلقائياً حتى يراجع الشركاء من جديد.

**سجل الرقابة.** كل عملية تُسجَّل مع القيم قبل وبعد: إنشاء، تعديل، إلغاء، حذف، موافقة، دخول، محاولة دخول فاشلة، نسخ احتياطي.
لا توجد أي شاشة لتعديل هذا السجل أو حذفه.

## الصلاحيات

| | مدير | محاسب | شريك | مشاهد |
|---|:-:|:-:|:-:|:-:|
| رؤية كل شيء | ✓ | ✓ | ✓ | ✓ |
| تسجيل الحركات وتعديلها وإلغاؤها | ✓ | ✓ | مصروف وإنتاج فقط، ويعدّل ما أدخله قبل ترحيله | — |
| الموافقة على المصاريف الكبيرة واعتماد الشهر | إذا رُبط بشريك | — | ✓ | — |
| الشركاء والنسب والمستخدمون والإقفال والنسخ الاحتياطي | ✓ | — | — | — |

## التشغيل

```bash
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8000
```

أول زيارة تفتح صفحة إنشاء حساب المدير. بعدها تظهر في الرئيسية خطوات البدء بالترتيب.

متغيرات البيئة:
- `PA_DATA_DIR`: مجلد قاعدة البيانات والمرفقات، والافتراضي `./data`.
- `PA_HOST` و`PA_PORT`.
- `PA_SECRET_KEY`: يُولَّد تلقائياً إن لم يُحدَّد.
- `PA_SECURE_COOKIES=1`: فعّله عند التشغيل خلف HTTPS، وهو مطلوب على الإنترنت.

**النسخ الاحتياطي:** من الإعدادات ← «تنزيل نسخة احتياطية» (ملف SQLite كامل)، أو انسخ مجلد `data/`.
احفظ نسخة أسبوعياً على الأقل خارج الخادم.

## الرفع على خادم (VPS بنظام Ubuntu/Debian)

السكربت `deploy/install.sh` يجهّز كل شيء بأمر واحد، ولا يمس أي مشروع آخر موجود على الخادم:
- يعمل بمستخدم خاص على منفذ داخلي خاص به (8077).
- يضيف ملف nginx منفصلاً، ويختبر الإعدادات قبل تطبيقها ويتراجع إن فشلت.
- يفعّل HTTPS، ونسخاً احتياطياً يومياً يُحفظ 60 يوماً في `/var/backups/partnership-accounting`.

```bash
# 1) مفتاح للقراءة فقط من المستودع الخاص (يُضاف في GitHub: Settings → Deploy keys)
ssh-keygen -t ed25519 -f /root/.ssh/pa_deploy -N ""
cat /root/.ssh/pa_deploy.pub
# 2) نسخ المشروع إلى /opt
GIT_SSH_COMMAND="ssh -i /root/.ssh/pa_deploy -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:engmahmoudkhella/partnership-accounting.git /opt/partnership-accounting
git -C /opt/partnership-accounting config core.sshCommand "ssh -i /root/.ssh/pa_deploy"
# 3) التنصيب: بدون نطاق يعمل على https://IP:8443 ، ومع نطاق: DOMAIN=accounts.example.com
bash /opt/partnership-accounting/deploy/install.sh
```

التحديث لاحقاً: `bash /opt/partnership-accounting/deploy/update.sh` (يأخذ نسخة احتياطية أولاً).
السجلات: `journalctl -u partnership-accounting -f`.

## الاختبارات

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests                  # 414 اختباراً
PROP_EXAMPLES=400 python -m pytest tests/test_property.py   # تشغيل عشوائي مكثف
node e2e/run.js                            # متصفح حقيقي (Playwright)
```

| الملف | ماذا يختبر |
|---|---|
| `test_numbers.py` | أكثر من 140 حالة إدخال أرقام: عربي وإنجليزي وفارسي ومختلط، الفواصل، العلامات المخفية، العملة، كل صيغة خاطئة، التقريب، تقسيم المبالغ |
| `test_opening.py` | الأرصدة الافتتاحية: التوازن حتى الأغورة، الأصول، التعديل، التواريخ، الإقفال، الإلغاء |
| `test_documents.py` | القيد الناتج عن كل نوع حركة، وكل قاعدة تحقق، والسحب على المكشوف بأثر رجعي |
| `test_void_delete_edit.py` | الإلغاء يعيد كل رصيد كما كان تماماً، الإلغاء المرفوض وأسبابه، الحذف النهائي، التعديل، إلغاء الاعتمادات، حذف الشركاء والحسابات والمستخدمين |
| `test_approvals_locks.py` | حد الموافقة، الرفض، إعادة الجولة، الترحيل المؤجل، إقفال الأشهر واعتمادها |
| `test_property.py` | مئات التسلسلات العشوائية من العمليات (أغلبها خاطئ عمداً)، ويُتحقق بعد كل خطوة من كل الضوابط، ومنها أن لا يستلم شريك أكثر من ربحه |
| `test_http.py` | النماذج الحقيقية بالعربية والإنجليزية، رسائل الخطأ، الصلاحيات، CSRF، رفع الملفات، اكتمال الترجمة في كل صفحة، التصدير |
| `e2e/run.js` | جولة كاملة في Chromium: إعداد، شركاء، أرصدة، مصاريف، إلغاء، إنجليزي، جوال |

---

<a name="english"></a>
## English

### What it records
Opening balances, capital calls (split by share), capital contributions, partner-to-partner transfers, expenses (from a
company account or paid personally by a partner), asset purchases and sales (gain/loss computed), invoices to the
customer (quantity × unit price), payments received (with cash-out commission), transfers between accounts (with fee),
profit distributions (split by share, capped at collected profit) and daily production (for cost per unit).

### Accuracy rules
- **Numbers**: Arabic-Indic, Persian, ASCII and mixed digits; `.`/`٫` decimal; `,` `٬` `،` space as thousands separators
  only in valid groups of three, so `12,5` is rejected with a hint instead of being read as 125. Nothing is rounded
  silently, and money never accepts a third decimal (`1.500` is refused with "did you mean 1500?").
  Every amount is stored as integer agorot. Each money field previews what will be saved as you type.
- **Shares**: an exact whole-number ratio (3:1:1:1) or percentages totalling 100%, with effective dates. Every split
  uses the largest-remainder method, so parts always add up to the agora; profit is split once per share period so
  rounding never accumulates. A distribution pays each partner from his own undistributed profit, so after a change
  of shares everyone still receives what he earned, and nobody can receive more than his share.
- **Deletion**: a posted transaction is never deleted; it is *voided* with a reason, which removes its effect exactly
  and keeps it visible. A void is refused when it would break the books (money already spent, invoice already paid,
  profit already distributed, asset already sold, call already paid). Only unposted (pending/rejected) transactions can
  be deleted, and that is logged with a full snapshot. Master records are deleted only if unused; otherwise deactivated.
- **Invariants after every operation**: debits = credits; assets = equity; no account, receivable, partner capital or
  asset negative on any day; distributions ≤ collected profit. A failing operation leaves nothing behind.
- **Approvals**: expenses/asset purchases above a threshold need every partner's approval; one rejection rejects; any
  edit restarts the round.
- **Month close**: partners sign off, the admin locks months in order (and unlocks newest first), so no backdated
  change can alter a locked month; any change clears sign-offs.
- **Audit log** of every action with before/after values; no screen can edit it.

### Run
`pip install -r requirements.txt && python run.py`. Set `PA_SECURE_COOKIES=1` behind HTTPS. Data lives in `PA_DATA_DIR`
(default `./data`); back it up regularly (Settings → Download backup).

### Not included yet
Multiple currencies (everything is ILS), depreciation, supplier credit (payables), inventory tracking (materials are
expensed when bought).
