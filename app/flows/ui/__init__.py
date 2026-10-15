"""Flow алхмуудад зориулсан DevExpress Blazor UI туслахууд: цэс нээх, форм бөглөх, товч, grid.

Бүх selector/товчны нэр config-ийн `flow.selectors` / `flow.buttons`-оос ирнэ — сайт бүрт
config/sites/<host>.yaml-аар тааруулна.

Дэд модулиуд (import-ын дарааллаар): _js (браузерт ажиллах JS) → base (config туслах) → wait (хүлээлт,
алдааны цонх) → menu (цэс нээх) → page (товч, таб, жагсаалт, форм хаах) → lookup → lines (баримтын мөр)
→ fields (талбар, combo, хадгалах) → grid (мөр хайх/сонгох). Гаднаас `from app.flows import ui; ui.X` гэж
хэрэглэнэ — бүх нэр энд дахин экспортлогдоно."""

from app.flows.ui._js import (  # noqa: F401
    FORM_TAB_JS,
    CLICK_OPTION_JS,
    EDIT_ROW_JS,
    FIND_BUTTON_JS,
    FIND_FIELD_JS,
    FIND_ROWS_JS,
    MARK_CELL_LOOKUP_JS,
    MARK_HEADER_CLOSE_JS,
    MARK_LINES_ADD_JS,
    MARK_LOOKUP_ROW_JS,
    MARK_SEARCH_JS,
    PROBE_JS,
    SCAN_FORM_JS,
    TOP_DIALOG_LEN_JS,
    _COMMON_JS,
)
from app.flows.ui.base import (  # noqa: F401
    buttons,
    fcfg,
    menu_item,
    sel_payload,
)
from app.flows.ui.wait import (  # noqa: F401
    popup_problems,
    raise_on_problems,
    settle,
    _is_error_text,
)
from app.flows.ui.menu import (  # noqa: F401
    ensure_menu,
    open_menu,
    open_menu_any,
    reload_app,
)
from app.flows.ui.page import (  # noqa: F401
    action_buttons,
    ACTION_BUTTONS_JS,
    BUTTON_EXISTS_JS,
    clear_grid_search,
    clear_toasts,
    click_button,
    click_popup_item,
    close_form,
    close_tabs,
    dialog_count,
    form_has_inputs,
    has_button,
    is_form_page,
    LIST_EMPTY_JS,
    MARK_FILTER_DATE_JS,
    mark_first_row,
    MARK_FIRST_ROW_JS,
    mark_seen,
    new_dom_hint,
    popup_dom_hint,
    POPUP_ITEMS_JS,
    popup_menu_items,
    probe,
    refresh_list,
    safe_type_items,
    scan_form,
    select_side_group,
    SIDE_GROUP_ROW_JS,
    tab_names,
    toolbar_dropdowns,
    TOOLBAR_DROPDOWNS_JS,
    widen_date_filter,
    _MAIN_GRID_JS,
)
from app.flows.ui.lookup import (  # noqa: F401
    pick_from_lookup,
    validation_targets,
    _lookup_search,
)
from app.flows.ui.lines import (  # noqa: F401
    CELL_EDITOR_JS,
    COMPUTED_WORDS,
    CREDIT_WORDS,
    DEBIT_WORDS,
    fill_lines,
    QTY_WORDS,
    _activate_cell,
    _cell_has_editor,
    _dump_line_row,
    _edit_row,
    _fill_cell,
)
from app.flows.ui.fields import (  # noqa: F401
    choose_combo,
    click_tab,
    CLICK_TAB_JS,
    fill_field,
    find_field,
    form_tabs,
    FORM_TABS_JS,
    line_column,
    LIST_OPTIONS_JS,
    MARK_DROPDOWN_BTN_JS,
    peek_combo,
    peek_line_lookups,
    pick_option,
    read_field,
    reveal_field,
    save_form,
)
from app.flows.ui.grid import (  # noqa: F401
    click_icon,
    CLICK_ICON_JS,
    click_text_any,
    CLICK_TEXT_ANY_JS,
    dialog_row,
    focus_check,
    FOCUS_CHECK_JS,
    form_has_text,
    FORM_HAS_TEXT_JS,
    form_values,
    FORM_VALUES_JS,
    grid_find,
    grid_find_settled,
    reload_list,
    MARK_ANY_ROW_JS,
    MARK_ROW_TARGET_JS,
    only_tagged_focus,
    row_check,
    RowSelection,
    select_row,
    _SCOPE_JS,
)
