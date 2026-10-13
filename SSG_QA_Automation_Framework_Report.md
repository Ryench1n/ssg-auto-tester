# SSG QA Automation Framework — Нэгтгэсэн бүрэн тайлан

## Юу вэ?

Энэ бол frontend-гүй, зөвхөн код/terminal түвшинд ажилладаг AI-туслалцаатай QA automation framework (C#/.NET/NUnit/Playwright суурьтай). Claude/ChatGPT-г AI pair-tester болгож ашиглаж, QA Hub, SmartAccounting, Quality Dashboard зэрэг SSG-ийн олон системд E2E тест бичиж, ажиллуулж, SSG стандарт загвараар тайлагнадаг.

**Яагаад frontend-гүй гэж хэлдэг вэ:** Хэрэглэгч чат хэлбэрээр даалгавар өгдөг → AI terminal/dotnet CLI/PowerShell ашиглан код бичиж, файл засварладаг → үр дүн нь Excel/Word/JSON/Markdown файлууд, terminal гаралт байдаг — тусгай веб interface байхгүй. AI өөрөө тестлэгдэж буй системүүдийн frontend-ийг шалгадаг, гэхдээ өөрөө ямар ч UI-гүй.

## 1. Архитектур (3 давхарга)

```
framework/  ← Нийтлэг, системээс үл хамаарах
  ├── Configuration (Env.cs)
  ├── Browser lifecycle, Authentication
  ├── Evidence (screenshot/trace)
  ├── Reporting (CaseTracker, Redactor)
  └── TestData
systems/<нэр>/  ← Систем тус бүрийн онцлог
  ├── Pages/, Tests/, Api/, system.json
templates/SSG/  ← Тайлангийн загвар
```

Зарчим: `framework/` нь URL, компани, endpoint огт мэдэхгүй байна; систем тус бүрийн хавтас өөрийн онцлогийг хариуцна.

## 2. Ажиллагааны 4 үе шат

| Үе шат | Юу хийдэг |
|---|---|
| **EXPLORE** | Unauthenticated, read-only судалгаа — UI, API, mutation байгаа эсэхийг ялгаж, код бичихээс өмнө зөвшөөрөл авна |
| **GENERATE** | BasePage-ээс удамшсан Page Object, `[SsgCase("XXX-01")]` тэмдэглэгээтэй тест бичих; mutation-guard helper-ээр л бичих үйлдэл дамжина |
| **VALIDATE** | `dotnet test`, регресс шалгалт (хуучин тестүүд Pass хэвээр), headed/headless давхар баталгаажуулалт |
| **ТАЙЛАН** | `framework-improvement-YYYY-MM-DD.md` — юу нэмэгдсэн, дахин ашиглалтын хувь, БАТАЛГААЖААГҮЙ жагсаалт |

## 3. Гол дэд системүүд

- **CaseTracker:** тест бүрийн `[TearDown]`-д SSG Case ID-тай Pass/Fail/Blocked/Pending бичдэг (муу төлөв давамгайлна: Fail > Blocked > Pending > Pass)
- **Mutation Guard:** `Env.AssertMutationAllowed()` — зөвхөн `.env`-д зөвшөөрөгдсөн host дээр л бичих үйлдэл хийнэ (өгөгдмөл: localhost)
- **Redactor:** token/нууц үгийг автоматаар маскладаг (дутагдал: `trace.zip` дотор хүрдэггүй)
- **Auth** (систем бүрээр ялгаатай): SmartAccounting — хүний оролцоотой AuthBootstrap (CAPTCHA тойрохгүй); QA Hub — API login → localStorage бичдэг; Dashboard — ганц нууц үг, sessionStorage

## 4. Дахин ашиглалтын хэмжилт

Custom Python скрипт ашиглан framework-ийн код мөр vs системд зориулж шинээр бичсэн мөрийг харьцуулж хувиар гаргадаг (жишээ: Dashboard-д 59.7%).

## 5. Аюулгүй байдал

- Нууц үг/token хэзээ ч git, лог, console-д задрахгүй (Redactor + `.env` git-д орохгүй)
- Delete функц санаатайгаар байхгүй
- QA prefix-тэй өгөгдлийг л цэвэрлэх зарчим (бүрэн хэрэгжээгүй)

## 6. SSG стандарт тайлангийн гарц

- `05_Test-Plan`, `07_Test-Cases` (xlsx), `09_Test-Report` (docx)
- Цөм зарчим: баталгаагүй зүйлийг хэзээ ч Pass гэж тэмдэглэдэггүй — **"БАТАЛГААЖААГҮЙ"** гэж ил тод ялгана

## AI (Claude) хийж чаддаг бүх ур чадвар

- **Веб судалгаа** — headless/headed browser-оор нэвтэрч UI/DOM/selector судлах, API endpoint илрүүлэх, mutation боломж ялгах
- **Код бичих/засах** — C#/JS/Python/PowerShell дээр Page Object, NUnit тест, framework-ийн дэд бүтэц өргөтгөх
- **Тест ажиллуулах, шинжлэх** — `dotnet test`/`build`, Pass/Fail/Blocked ялгах, root cause тодорхойлох, TRX/XML/JSON задлах
- **Файл/зураг унших** — screenshot шинжлэх, .zip/.z01 архив, docx/xlsx баримт унших
- **Аудит** — эдит хийхгүйгээр статик аудит (P1/P2 зэрэглэл), аюулгүй байдлын цоорхой илрүүлэх, дахин ашиглалт хэмжих
- **Тайлан бичих** — SSG загвар, README/CLAUDE.md шинэчлэх, дэлгэрэнгүй тайлан үйлдвэрлэх
- **Олон хэл** — Монгол (кирилл) дээр бүрэн ажиллах
- **Эрсдэл удирдах** — mutation-ийн өмнө зөвшөөрөл авах, БАТАЛГААЖААГҮЙ зүйлийг ил тод ялгах
- **Session удирдлага** — usage limit тасарсны дараа ул мөр хайж, тасарсан газраасаа үргэлжлүүлэх
