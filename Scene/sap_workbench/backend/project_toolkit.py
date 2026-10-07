"""项目目录内的 SAP 能力产物：skill 与插件（幂等安装）。

为什么不放在场景自管宿主里：工作台的对话现在由平台的标准编码会话承载，
标准 OpenCode 服务**天然**会从项目目录发现 skill 与插件：

* skill：配置目录下的 `{skill,skills}/**/SKILL.md`（`<project>/.opencode` 是配置目录）；
* 插件：配置目录下的 `{plugin,plugins}/*.{js,ts}`，由 `ConfigPlugin.load` 自动发现。

因此场景只需要把文件写进项目的 `.opencode/`，不需要场景专用构建，也不需要在
项目配置里登记插件。

幂等规则（与既有 `opencode_adapter/native-host.ts` 的 `installNavigationGuidance`
同源，因为旧宿主安装的是**同名文件**，迁移必须能替换它）：

1. 文件不存在 → 写入；
2. 已存在且内容相同（忽略 BOM 与行尾差异）→ 保留，不重写；
3. 已存在且内容是我们**曾经发布过**的修订 → 升级为新版本；
4. 已存在但是别的内容（用户改过）→ **拒绝覆盖**，如实报告差异。

第 4 条是刻意的：用户对项目产物的改动不得被静默丢弃。
"""
from hashlib import sha256
import json
from pathlib import Path

#: 产物随场景代码发布，路径相对场景包。
_ROOT = Path(__file__).resolve().parents[1] / 'project'

#: 要安装的产物：来源（相对场景包）→ 项目内的落点。
ARTIFACTS = (
    ('skills/sap-workbench/SKILL.md', '.opencode/skills/sap-workbench/SKILL.md'),
    ('skills/sap-workbench/references/boundaries.md',
     '.opencode/skills/sap-workbench/references/boundaries.md'),
    ('skills/sap-workbench/references/transactions.md',
     '.opencode/skills/sap-workbench/references/transactions.md'),
    ('plugins/rsm-sap-workbench-navigation.js',
     '.opencode/plugins/rsm-sap-workbench-navigation.js'),
)

#: 默认拒绝规则写在项目的 OpenCode 配置里。插件与 skill 对该项目的**所有**会话可见
#: （包括普通编码对话），只有权限规则能把工具收窄：`action: "deny"` 会让模型根本
#: 看不到 `sap_transaction_open`（`resolveTools` → `Permission.disabled`）。场景发起的
#: 会话由平台入口下发**会话级** allow 重新放开（见 `agent.coding.permissions`）。
#: 这只是减少误调用，不是授权依据——每次调用仍由服务端按会话归属校验。
CONFIG_FILE = '.opencode/opencode.json'
#: Tools the project config denies by default. Both ship into the same project
#: directory, so both are visible to *every* session of that project; only a
#: scene-issued session re-allows them (see ``agent.coding.permissions``).
#: ``sap_data_call`` reaches business data and, through ``call_rfc``, BAPIs that
#: write business documents -- denied by default for the same reason as
#: navigation, and re-allowed only for the sessions the scene names.
HIDDEN_TOOLS = ('sap_transaction_open', 'sap_data_call', 'sap_page_read')

#: 我们发布过的历史修订摘要（规范化后）。由旧宿主
#: (`opencode_adapter/native-host.ts`) 写入的同名插件摘要在此登记，否则已有项目里
#: 那份文件既不是新内容、也不是"用户改动"，迁移会被误判为冲突而拒绝替换。
#: 规则同旧实现：每替换一次修订，就把被替换版本的摘要加进来，并保留最早的条目。
UPGRADEABLE_REVISIONS = frozenset({
    # Last deployed page reader, before explicit current-user observations.
    '1151c54b4e06a41a6d0351c4090b7cb6800e6d8ca45256bb16c26120bb7e1b85',
    '93e7bd4bdacb8db2b75c2b5a2e6e20fd4be742049833bd0fffa3b35b1f19beea',

    # First macOS reader release, before client/server platform clarification.
    "5c1db5fd19742295e84fe1e5438e0eab8da081f940cafc43f6624fd8de5a6755",
    "5b723e591d8648fd938930cae0d64a3cb20976c4dd589b393174881424b5b382",
    "47f8bac4fb306271647fb88cb2a53d43c8533e5d996e650f8c39c148e68ffac2",
    # Last shipped plugin and guidance, before macOS page reading.
    'd7f760c873afbac44527b99b8a770c90c75730ac821cfa2c2ce04f6924fc90e1',
    '779b8db19a1caaa85a904a5d8d5c7bda7562a30aa407180345ed58e7bb5b0994',
    'f45706ce7096351c83e6a89aae209230bcf4124c1d11bb8718ecdf16b84e9eeb',
    # 旧宿主发布过、由 `guidanceDigest` 计算的规范化摘要
    'a4d3587833ee220f3f64e9271acccc386ecbe6a03774adbc9931c2f19635af32',
    'a40f3755d6975426f9b7f424628d712f25e98b23dae64e09923bda0df93a7bab',
    # The revision the per-session host installs **today** (`navigation-guidance.js`,
    # digest 0412f4ef...). The two entries above are earlier revisions it replaced,
    # so they are not what a currently deployed host actually has on disk: the
    # common case is exactly this one. Without it, migrating a live install would
    # read the host's own file as a user edit and refuse to replace it.
    # Hard-coded on purpose: it must outlive the file it was computed from.
    '0412f4efe704df384f677e8705aa7b228c88b0e3263607554d1facce7ff4ade7',
    # The revision the toolkit itself installed before the scene-mediated
    # business-data channel was added: the plugin gained `sap_data_call`, and
    # SKILL.md / boundaries.md gained its boundary. A live project still holds
    # exactly these bytes, so without them the first install after this change
    # would read our own previous output as a user edit and refuse to replace
    # it -- the silent-stale-deployment shape task 8.5 already hit once.
    # Computed from the installed files, not from these sources, because those
    # bytes are what a live project actually has on disk.
    '647c3bbef423bd19aaba6ea0a5bb837c380ee645a402768017d18928efffd206',  # plugins/rsm-sap-workbench-navigation.js
    '5c33fde00b6da78627b136c457e526b23d2b9ef0cda1694f293e49af4b93409c',  # skills/sap-workbench/SKILL.md
    '5dc9c6bb186cb538986d2021db5149c2e3c07d62261c9654860d887d83473fdd',  # .../references/boundaries.md
})


def normalise(text):
    """去掉 BOM、统一行尾。同一修订在 Windows 检出上是 CRLF，按原始字节比较会把
    完全相同的修订判成"外来文件"并阻断启动。"""
    if text.startswith('\ufeff'):
        text = text[1:]
    return text.replace('\r\n', '\n').replace('\r', '\n')


def revision_digest(text):
    return sha256(normalise(text).encode('utf-8')).hexdigest()


def _install_one(source, target):
    """安装一个产物；返回 'installed' / 'unchanged' / 'upgraded' / 'conflict'。"""
    content = source.read_text(encoding='utf-8')
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return 'installed'
    previous = target.read_text(encoding='utf-8')
    if normalise(previous) == normalise(content):
        return 'unchanged'
    if revision_digest(previous) in UPGRADEABLE_REVISIONS:
        target.write_text(content, encoding='utf-8')
        return 'upgraded'
    return 'conflict'


def _ensure_hidden_tool(root):
    """把默认拒绝规则写进项目的 OpenCode 配置。返回如下之一：

    * ``installed`` —— 新建了配置文件，或把缺的键补进去了；
    * ``unchanged`` —— 程序化写入的配置已含我们的全部规则（幂等）；
    * ``user_set`` —— 用户已显式给某个工具设过不同的动作，**不覆盖**；即便另一个
      工具的规则仍需补写，也返回这一项，因为"某条拒绝可能没生效"才是调用方更需要
      知道的事；
    * ``unmergeable`` —— 既有配置不是纯 JSON（含注释的 JSONC），无法在不破坏它
      的前提下合并；如实报告，界面不得据此宣称工具已收窄。

    第 3、4 条是刻意的：用户对项目配置的改动不得被静默丢弃，宁可如实报告。
    """
    path = root / CONFIG_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            '$schema': 'https://opencode.ai/config.json',
            'permission': {tool: 'deny' for tool in HIDDEN_TOOLS},
        }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return 'installed'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError, UnicodeDecodeError):
        # JSONC (comments) or a malformed file: a blind rewrite would destroy the
        # user's comments, so refuse the merge rather than mangle the file.
        return 'unmergeable'
    if not isinstance(data, dict):
        return 'unmergeable'
    permission = data.get('permission')
    if permission is None:
        permission = data['permission'] = {}
    elif not isinstance(permission, dict):
        # A bare action string ("permission": "allow") is a different shape:
        # adding a per-tool key would rewrite the whole rule.
        return 'unmergeable'
    added = user_set = False
    for tool in HIDDEN_TOOLS:
        if tool in permission:
            # Already there (ours, or the user's own identical choice) → the
            # rule is in effect; anything else is an explicit user decision we
            # must not fight.
            if permission[tool] != 'deny':
                user_set = True
            continue
        permission[tool] = 'deny'
        added = True
    if user_set:
        outcome = 'user_set'
    elif added:
        outcome = 'installed'
    else:
        outcome = 'unchanged'
    if outcome != 'unchanged':
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n',
                        encoding='utf-8')
    return outcome


def install(project_dir):
    """把 skill 与插件写入 `project_dir/.opencode/`；幂等，且不覆盖用户改动。

    返回逐项结果，调用方据此如实报告（界面不得以"工具已注册"宣称能力可用）。
    """
    root = Path(project_dir)
    if not root.is_dir():
        raise ValueError('project_directory_missing')
    results = []
    for relative, destination in ARTIFACTS:
        source = _ROOT / relative
        if not source.is_file():
            results.append({'path': destination, 'result': 'missing_source'})
            continue
        results.append({'path': destination, 'result': _install_one(source, root / destination)})
    permission = _ensure_hidden_tool(root)
    return {'artifacts': results, 'permission': permission,
            'conflicts': [item['path'] for item in results if item['result'] == 'conflict']}
