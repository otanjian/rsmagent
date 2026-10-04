"""Closed automatic-action effects for the first SAP screen profiles.

Titles identify a compatible screen shape, never an identity or permission.
All authorization still belongs to the bound runtime. Unknown/customized
screens and persistence effects remain manual until separately accepted.
"""
import re
from types import MappingProxyType

from ..backend.configuration import WorkbenchError


TRANSACTIONS = MappingProxyType({'SPRO': 'customizing_navigation', 'ME21N': 'purchase_order_draft'})
PROFILES = MappingProxyType({
    'SAP 轻松访问': 'home', 'SAP Easy Access': 'home',
    '定制：执行项目': 'spro_entry', 'Customizing: Execute Project': 'spro_entry',
    '显示实施指南': 'spro_img', 'Display IMG': 'spro_img',
    '创建采购订单': 'me21n_create', 'Create Purchase Order': 'me21n_create',
})
EFFECTS = MappingProxyType({
    'read': ('observation', 'read_only'),
    'navigate': ('replace_screen', 'may_discard_draft'),
    'fill': ('edit_unsaved_field', 'draft_change'),
    'reference_img': ('display_reference_img', 'read_only_navigation'),
    'expand': ('expand_img_node', 'display_change'),
    'collapse': ('collapse_img_node', 'display_change'),
    'help': ('open_value_help', 'read_only_query'),
    'choose': ('select_help_value', 'draft_change'),
    'dismiss': ('close_owned_value_help', 'display_change'),
    'validate': ('check_purchase_order', 'validation_only'),
    'scroll': ('scroll_purchase_order_grid', 'display_change'),
})
SCROLL_STEPS = MappingProxyType({'up': (0, -320), 'down': (0, 320), 'left': (-240, 0), 'right': (240, 0)})
PERSISTENCE = re.compile(r'保存|儲存|存储|存儲|过账|過帳|删除|刪除|暂存|暫存|保留|提交|save|post|delete|commit|park|hold|submit', re.I)
# Explicit observed/display shortcut variants, not arbitrary parenthetical text.
CHECK = re.compile(r'(?:检查|檢查|Check)(?:\s*\((?:Ctrl\+F3|Ctrl\+Shift\+F3|Cmd Shift F3)\))?', re.I)
IMG = re.compile(r'(?:(?:显示|顯示|Display)\s*)?SAP\s*(?:参考|參考|Reference)\s*IMG(?:\s*\(F5\))?', re.I)
VALUE_HELP = re.compile(r'(?:值帮助|值幫助|输入帮助|輸入幫助|Value Help|Input Help|Possible Entries)(?:\s*[:：]\s*.{1,100})?', re.I)
# The observed supplier F4 window falls back to its whole innerText for its
# label. This closed structure permits opening/reading and owned Escape only;
# the seven filter labels do not prove their input values, or authorize a query.
SUPPLIER_RESTRICTION_LINES = (
    '限制值范围 (1)', '搜索并选择', 'A: 供应商（常规）', '执行', '加重', '隐藏过滤器',
    '搜索词:', '国家/地区代码:', '邮政编码:', '城市:', '名称:', '供应商:', '集中删除标志:',
    '项目 (0)', '取消',
)


def supplier_restriction_dialog(dialog):
    """Recognize the complete recorded Chinese window, never a title prefix."""
    if not isinstance(dialog, dict):
        return False

    def lines(value, limit):
        # Hitting a SNAPSHOT slice bound cannot establish complete content.
        if not isinstance(value, str) or len(value) >= limit:
            return ()
        return tuple(re.sub(r'\s+', ' ', line).strip()
                     for line in value.splitlines() if line.strip())

    return (lines(dialog.get('label'), 240) == SUPPLIER_RESTRICTION_LINES
            and lines(dialog.get('text'), 2000) == SUPPLIER_RESTRICTION_LINES)


def profile(page):
    return PROFILES.get(page.get('title', '').strip())


def effect(operation, page, *, transaction=None):
    if operation not in EFFECTS:
        raise WorkbenchError('action_forbidden', 403)
    name, risk = EFFECTS[operation]
    return {'operation': operation, 'effect': name, 'risk': risk,
            'profile': profile(page) or 'unsupported',
            **({'transaction': transaction} if transaction else {}), 'submits': False}


def navigation(transaction, page, *, dirty):
    if transaction not in TRANSACTIONS:
        raise WorkbenchError('transaction_unsupported', 400)
    if page.get('dialogs'):
        raise WorkbenchError('navigation_context_changed', 409)
    if profile(page) is None:
        raise WorkbenchError('page_effect_unsupported', 409)
    if dirty:
        raise WorkbenchError('draft_navigation_forbidden', 409)


def require_draft(page):
    if profile(page) != 'me21n_create' or page.get('dialogs'):
        raise WorkbenchError('page_effect_unsupported', 409)


def value_help_dialog(page):
    dialogs = page.get('dialogs', [])
    if len(dialogs) != 1 or profile(page) != 'me21n_create':
        return None
    dialog = dialogs[0]
    if supplier_restriction_dialog(dialog):
        # "集中删除标志" is one exact observed filter label. The generic
        # persistence prohibition remains intact for every other dialog.
        return dialog
    if not VALUE_HELP.fullmatch(dialog.get('label', '').strip()) or PERSISTENCE.search(dialog.get('text', '')):
        return None
    return dialog


def supported_control(operation, control, page):
    """Mouse and keyboard paths use the same effect classification."""
    label = re.sub(r'\s+', ' ', control.get('label', '')).strip()
    role, scope = control.get('role'), profile(page)
    if PERSISTENCE.search(label):
        return False
    if operation == 'reference_img':
        return scope == 'spro_entry' and role == 'button' and bool(IMG.fullmatch(label)) and not page.get('dialogs')
    if operation in {'expand', 'collapse'}:
        root = re.search(r'SAP\s*(?:用户化实施指南|Customizing Implementation Guide)', page.get('text', ''), re.I)
        return scope == 'spro_img' and bool(root) and role == 'treeitem' and control.get('expanded') in {'true', 'false'} and not page.get('dialogs')
    if operation == 'validate':
        return scope == 'me21n_create' and role == 'button' and bool(CHECK.fullmatch(label)) and not page.get('dialogs')
    if operation == 'choose':
        return (scope == 'me21n_create' and role == 'option' and bool(control.get('popup'))
                and bool(page.get('_owned_value_help')) and isinstance(control.get('value'), str)
                and not any(supplier_restriction_dialog(d) for d in page.get('dialogs', [])))
    # No tab identity/effect baseline has been accepted yet.
    return False
