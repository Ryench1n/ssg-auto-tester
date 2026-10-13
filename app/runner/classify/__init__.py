"""Илрэл → signature → Bug, эрэмбэ, засах санал; цэс → Test Case; GO/NO-GO.

Дэд модулиуд (import-ын дарааллаар): signature → severity (эрэмбэ, гарчиг, засах санал) → modules
(модулийн товчлол) → testcases → bugs → verdict (статистик, GO/NO-GO). Гаднаас
`from app.runner.classify import X` гэж хэрэглэнэ — бүх нэр энд дахин экспортлогдоно."""

from app.runner.classify.signature import (  # noqa: F401
    first_exception_line,
    normalize_text,
    signature,
    _GUID,
    _HEX,
    _NUM,
    _url_pattern,
    _WS,
)
from app.runner.classify.severity import (  # noqa: F401
    BASE_SEVERITY,
    fix_for,
    FLOW_CRITICAL_KINDS,
    FLOW_EXPECTED,
    FORM_STEPS,
    security_note_for,
    severity_for,
    title_for,
    _RAW_PATTERNS,
)
from app.runner.classify.modules import (  # noqa: F401
    auto_module_code,
    ModuleCoder,
    transliterate,
    _TRANSLIT,
)
from app.runner.classify.testcases import (  # noqa: F401
    build_test_cases,
    flow_step_key,
    is_form_flow,
    tested_flows,
    _default_priority,
    _fmt_dur,
    _form_tcs,
    _is_critical_item,
    _short_note,
)
from app.runner.classify.bugs import (  # noqa: F401
    bug_candidates,
    build_bugs,
    flow_signature,
    form_field_stats,
    root_causes,
    _EMPTY_WARN_RE,
    _flow_bugs,
    _form_bugs,
    _LOADED_RE,
    _ROOT_PATTERNS,
)
from app.runner.classify.verdict import (  # noqa: F401
    go_no_go,
    stats,
    template_v4_expected,
)
