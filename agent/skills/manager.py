"""
Skill manager for managing skill lifecycle and operations.
"""

import os
import json
import threading
from typing import Dict, Iterable, List, Optional
from common.log import logger
from agent.skills.types import Skill, SkillEntry, SkillSnapshot
from agent.skills.loader import SkillLoader
from agent.skills.formatter import format_skill_entries_for_prompt

SKILLS_CONFIG_FILE = "skills_config.json"
SKILLS_CONFIG_TMP_PREFIX = f".{SKILLS_CONFIG_FILE}."


class SkillNameAmbiguous(ValueError):
    """A bare skill ``name`` names more than one definition; ``resource_id`` needed.

    A ``ValueError`` so the existing not-found/validation handlers keep working,
    and its own type so the API can answer 400 for exactly this case instead of
    reporting a generic failure.
    """

    def __init__(self, name: str, sources: List[str]):
        unique = sorted({s for s in sources if s})
        where = " and ".join(unique) if unique else "more than one source"
        super().__init__(
            f"skill name {name!r} is ambiguous ({where}); "
            "pass resource_id to disambiguate")
        self.name = name
        self.sources = unique


def build_skill_manager(
    agent_id: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    config: Optional[Dict] = None,
) -> "SkillManager":
    """Build a manager pointed at the skills one Agent actually draws on.

    Resolved through ``state_dir`` rather than joined with ``"skills"``: an
    Agent without a directory of its own falls back to the shared one, which is
    what makes an installed skill reachable from every Agent. The profile's
    selection is what still lets them differ.
    """
    from common import state_dir
    from common.runtime_identity import RuntimeIdentity, current_identity

    identity = (
        RuntimeIdentity(agent_id=agent_id) if agent_id else current_identity()
    )
    selection = None
    try:
        from agent.registry import get_agent_registry

        profile = get_agent_registry().get(
            identity.agent_id or None, require_enabled=False
        )
        selection = profile.skills
    except Exception:
        # An unresolvable Agent must not cost the caller its skills; the
        # unfiltered shared set is the pre-selection behaviour.
        pass

    if workspace_dir:
        custom_dir = state_dir.skills_dir(base=workspace_dir)
    else:
        custom_dir = state_dir.skills_dir(identity)
    return SkillManager(custom_dir=str(custom_dir), config=config, selection=selection)


class SkillManager:
    """Manages skills for an agent."""

    def __init__(
        self,
        builtin_dir: Optional[str] = None,
        custom_dir: Optional[str] = None,
        config: Optional[Dict] = None,
        selection: Optional[Iterable[str]] = None,
    ):
        """
        Initialize the skill manager.

        :param builtin_dir: Built-in skills directory (project root ``skills/``)
        :param custom_dir: Custom skills directory (workspace ``skills/``)
        :param config: Configuration dictionary
        :param selection: Names this Agent draws on. ``None`` means all of them.
        """
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.builtin_dir = builtin_dir or os.path.join(project_root, 'skills')
        self.custom_dir = custom_dir or os.path.join(project_root, 'workspace', 'skills')
        self.config = config or {}
        self._skills_config_path = os.path.join(self.custom_dir, SKILLS_CONFIG_FILE)
        # Which of the shared skills this Agent uses. Kept separate from
        # skills_config.json because that file describes the instance-wide
        # library, which every Agent reads and which one Agent turning a skill
        # off for itself must not rewrite.
        self.selection: Optional[set] = None if selection is None else set(selection)

        # skills_config: full skill metadata keyed by name
        # { "web-fetch": {"name": ..., "description": ..., "source": ..., "enabled": true}, ... }
        self.skills_config: Dict[str, dict] = {}

        self.loader = SkillLoader()
        self.skills: Dict[str, SkillEntry] = {}

        # Load skills on initialization
        self.refresh_skills(use_cache=True)

    def refresh_skills(self, use_cache: bool = False):
        """Reload all skills from builtin and custom directories, then sync config.

        :param use_cache: Reuse the parse of skill files unchanged on disk since
            they were last read. Directories are always rescanned.
        """
        self.skills = self.loader.load_all_skills(
            builtin_dir=self.builtin_dir,
            custom_dir=self.custom_dir,
            use_cache=use_cache,
        )
        self._sync_skills_config()
        logger.debug(f"SkillManager: Loaded {len(self.skills)} skills")

    # ------------------------------------------------------------------
    # skills_config.json management
    # ------------------------------------------------------------------
    def _load_skills_config(self) -> Optional[Dict[str, dict]]:
        """Load skills_config.json from custom_dir.

        Returns an empty dict if the file doesn't exist, and None if it exists
        but can't be read, so the caller never mistakes it for an empty config
        and writes the defaults over the user's choices.
        """
        if not os.path.exists(self._skills_config_path):
            return {}
        try:
            with open(self._skills_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning(f"[SkillManager] {SKILLS_CONFIG_FILE} is not a JSON object, left as is")
        except Exception as e:
            logger.warning(f"[SkillManager] Failed to load {SKILLS_CONFIG_FILE}: {e}")
        return None

    def _save_skills_config(self):
        """Persist skills_config to custom_dir/skills_config.json.

        Written to a temporary file and renamed into place, so a concurrent
        reader sees either the old file or the new one, never a partial write.
        """
        os.makedirs(self.custom_dir, exist_ok=True)
        tmp_path = os.path.join(
            self.custom_dir,
            f"{SKILLS_CONFIG_TMP_PREFIX}{os.getpid()}.{threading.get_ident()}.tmp",
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.skills_config, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, self._skills_config_path)
        except Exception as e:
            logger.error(f"[SkillManager] Failed to save {SKILLS_CONFIG_FILE}: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _sync_skills_config(self):
        """
        Merge directory-scanned skills with the persisted config file.

        - New skills: use metadata.default_enabled as initial enabled state.
        - Existing skills: preserve their persisted enabled state.
        - Skills that no longer exist on disk are removed.
        - name/description/source are always refreshed from the latest scan.
        """
        loaded = self._load_skills_config()
        saved = loaded if loaded is not None else self.skills_config
        merged: Dict[str, dict] = {}

        for name, entry in self.skills.items():
            skill = entry.skill
            prev = saved.get(name, {})
            category = prev.get("category", "skill")

            if name in saved:
                enabled = prev.get("enabled", True)
            else:
                enabled = entry.metadata.default_enabled if entry.metadata else True

            source = prev.get("source") or skill.source
            entry_dict = {
                "name": name,
                "description": skill.description,
                "source": source,
                "enabled": enabled,
                "category": category,
                # Stable namespace:source identity. A rename keeps this id; a
                # different source (builtin vs custom) never collides. This is
                # what resource authorization references.
                "resource_id": f"{source}:{name}",
            }
            display_name = prev.get("display_name")
            if display_name:
                entry_dict["display_name"] = display_name
            merged[name] = entry_dict

        self.skills_config = merged
        # Rewritten only when something changed: this runs before every run,
        # and an unreadable file is left for the user rather than replaced.
        if loaded is not None and merged != loaded:
            self._save_skills_config()

    def is_skill_enabled(self, name: str) -> bool:
        """
        Check if a skill is enabled for this Agent.

        A skill has to clear both gates: the instance-wide library switch in
        skills_config.json, and this Agent's own selection. Narrowing rather
        than overriding keeps "turn this off everywhere" working no matter which
        Agents happen to have selected it.

        :param name: skill name
        :return: True if enabled (default True if not in config)
        """
        if self.selection is not None and name not in self.selection:
            return False
        entry = self.skills_config.get(name)
        if entry is None:
            return True
        return entry.get("enabled", True)

    def set_skill_enabled(self, name: str, enabled: bool):
        """
        Set a skill's enabled state and persist.

        :param name: skill name
        :param enabled: True to enable, False to disable
        """
        if name not in self.skills_config:
            raise ValueError(f"skill '{name}' not found in config")
        self.skills_config[name]["enabled"] = enabled
        self._save_skills_config()

    def get_skills_config(self) -> Dict[str, dict]:
        """
        Return the full skills_config dict (for query API).

        :return: copy of skills_config
        """
        return dict(self.skills_config)
    
    def get_skill(self, name: str) -> Optional[SkillEntry]:
        """
        Get a skill by name.
        
        :param name: Skill name
        :return: SkillEntry or None if not found
        """
        return self.skills.get(name)
    
    def list_skills(self) -> List[SkillEntry]:
        """
        Get all loaded skills.
        
        :return: List of all skill entries
        """
        return list(self.skills.values())
    
    @staticmethod
    def _normalize_skill_filter(skill_filter: Optional[List[str]]) -> Optional[List[str]]:
        """Normalize a skill_filter list into a flat list of stripped names."""
        if skill_filter is None:
            return None
        normalized = []
        for item in skill_filter:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    normalized.append(name)
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, str):
                        name = subitem.strip()
                        if name:
                            normalized.append(name)
        return normalized or None

    def _authorized_skill_ids(self) -> Optional[set]:
        """Return the skill ``resource_id`` set the current identity may *use*.

        Returns ``None`` when runtime authorization is not in play (legacy mode
        or no identity), meaning "unrestricted". In database mode it intersects
        the ambient identity's ``skill.use`` grants so only authorized skills
        reach the prompt. Recomputed on every call — never cached across a
        request — so a grant granted mid-session takes effect immediately.
        """
        from common.runtime_identity import current_identity

        ident = current_identity()
        if not ident.user_id or not ident.tenant_id:
            return None
        try:
            from auth.service import get_identity_service
            svc = get_identity_service()
            ids = svc.resource_ids_for(ident.user_id, ident.tenant_id, "skill", "use",
                                       permission="skill.use")
        except Exception:
            return None
        # Platform admin / legacy derivation: resource_ids_for returns None for
        # "unrestricted".
        return ids

    def is_authorized(self, resource_id: str) -> bool:
        """Whether the current identity may *use* ``resource_id`` right now.

        The live, per-skill form of :meth:`_apply_skill_use_auth`, for callers
        that must re-check one skill at a time -- deploying a skill package
        (task 8.1/8.4) is a per-skill decision made after the prompt was built,
        so a grant revoked in between has to be visible here. ``True`` when
        runtime authorization is not in play (legacy mode or no identity), which
        matches :meth:`_authorized_skill_ids` returning ``None`` for
        "unrestricted".

        Matching mirrors :meth:`_apply_skill_use_auth`: an exact id, or the same
        trailing name under a different source namespace, so a grant does not
        turn into a silent denial when the runtime directory and the catalog
        disagree about the source.
        """
        allowed = self._authorized_skill_ids()
        if allowed is None:
            return True
        if resource_id in allowed:
            return True
        name = resource_id.split(":", 1)[-1]
        return any(rid.split(":", 1)[-1] == name for rid in allowed)

    def _apply_skill_use_auth(self, entries: List[SkillEntry]) -> List[SkillEntry]:
        """Narrow entries to those the current identity is allowed to use.

        A grant may reference a catalog ``resource_id`` (``{source}:{name}``).
        The runtime skill directory may report the same skill under a different
        source namespace (a workspace copy of a builtin, or a not-yet-copied
        builtin), so we match on the trailing ``:{name}`` as well as the exact id
        to avoid a grant turning into a silent denial when the two disagree.
        """
        allowed = self._authorized_skill_ids()
        if allowed is None:
            return entries
        names = {rid.split(":", 1)[-1] for rid in allowed}
        return [
            e for e in entries
            if self._skill_resource_id(e) in allowed or e.skill.name in names
        ]

    def _skill_resource_id(self, entry: SkillEntry) -> str:
        """Stable ``source:name`` id for a loaded skill entry."""
        source = self.skills_config.get(entry.skill.name, {}).get("source") or entry.skill.source
        ns = source if source in ("builtin", "custom") else "builtin"
        return f"{ns}:{entry.skill.name}"

    def filter_skills(
        self,
        skill_filter: Optional[List[str]] = None,
        include_disabled: bool = False,
    ) -> List[SkillEntry]:
        """
        Filter skills that are eligible (enabled + requirements met).

        :param skill_filter: List of skill names to include (None = all)
        :param include_disabled: Whether to include disabled skills
        :return: Filtered list of eligible skill entries
        """
        from agent.skills.config import should_include_skill

        entries = list(self.skills.values())

        entries = [e for e in entries if should_include_skill(e, self.config)]

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        if not include_disabled:
            entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        from config import conf
        if not conf().get("knowledge", True):
            entries = [e for e in entries if e.skill.name != "knowledge-wiki"]

        entries = self._apply_skill_use_auth(entries)

        return entries

    def filter_unavailable_skills(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> tuple:
        """
        Find skills that are enabled but have unmet requirements.

        :param skill_filter: Optional list of skill names to include
        :return: Tuple of (entries, missing_map) where missing_map maps
                 skill name to its missing requirements dict
        """
        from agent.skills.config import should_include_skill, get_missing_requirements

        entries = list(self.skills.values())

        # Only enabled skills
        entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        # A skill that is not authorized for use is not even advertised as
        # "unavailable" — it is simply not part of this identity's library.
        entries = self._apply_skill_use_auth(entries)

        # Keep only those that fail should_include_skill (requirements not met)
        unavailable = []
        missing_map: Dict[str, dict] = {}
        for e in entries:
            if not should_include_skill(e, self.config):
                missing = get_missing_requirements(e)
                if missing:
                    unavailable.append(e)
                    missing_map[e.skill.name] = missing

        return unavailable, missing_map

    def build_skills_prompt(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> str:
        """
        Build a formatted prompt containing available skills
        and brief hints for unavailable ones.

        :param skill_filter: Optional list of skill names to include
        :return: Formatted skills prompt
        """
        from common.log import logger
        from agent.skills.formatter import format_unavailable_skills_for_prompt

        eligible = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        logger.debug(f"[SkillManager] Eligible: {len(eligible)} skills (total: {len(self.skills)})")
        if eligible:
            skill_names = [e.skill.name for e in eligible]
            logger.debug(f"[SkillManager] Eligible skills: {skill_names}")

        result = format_skill_entries_for_prompt(eligible)

        unavailable, missing_map = self.filter_unavailable_skills(skill_filter=skill_filter)
        if unavailable:
            unavailable_names = [e.skill.name for e in unavailable]
            logger.debug(f"[SkillManager] Unavailable skills (setup needed): {unavailable_names}")
            result += format_unavailable_skills_for_prompt(unavailable, missing_map)

        logger.debug(f"[SkillManager] Generated prompt length: {len(result)}")
        return result
    
    def build_skill_snapshot(
        self,
        skill_filter: Optional[List[str]] = None,
        version: Optional[int] = None,
    ) -> SkillSnapshot:
        """
        Build a snapshot of skills for a specific run.
        
        :param skill_filter: Optional list of skill names to include
        :param version: Optional version number for the snapshot
        :return: SkillSnapshot
        """
        entries = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        prompt = format_skill_entries_for_prompt(entries)
        
        skills_info = []
        resolved_skills = []
        
        for entry in entries:
            skills_info.append({
                'name': entry.skill.name,
                'primary_env': entry.metadata.primary_env if entry.metadata else None,
            })
            resolved_skills.append(entry.skill)
        
        return SkillSnapshot(
            prompt=prompt,
            skills=skills_info,
            resolved_skills=resolved_skills,
            version=version,
        )
    
    def sync_skills_to_workspace(self, target_workspace_dir: str):
        """
        Sync all loaded skills to a target workspace directory.
        
        This is useful for sandbox environments where skills need to be copied.
        
        :param target_workspace_dir: Target workspace directory
        """
        import shutil
        
        target_skills_dir = os.path.join(target_workspace_dir, 'skills')
        
        # Remove existing skills directory
        if os.path.exists(target_skills_dir):
            shutil.rmtree(target_skills_dir)
        
        # Create new skills directory
        os.makedirs(target_skills_dir, exist_ok=True)
        
        # Copy each skill
        for entry in self.skills.values():
            skill_name = entry.skill.name
            source_dir = entry.skill.base_dir
            target_dir = os.path.join(target_skills_dir, skill_name)
            
            try:
                shutil.copytree(source_dir, target_dir)
                logger.debug(f"Synced skill '{skill_name}' to {target_dir}")
            except Exception as e:
                logger.warning(f"Failed to sync skill '{skill_name}': {e}")
        
        logger.info(f"Synced {len(self.skills)} skills to {target_skills_dir}")
    
    def get_skill_by_key(self, skill_key: str) -> Optional[SkillEntry]:
        """
        Get a skill by its skill key (which may differ from name).
        
        :param skill_key: Skill key to look up
        :return: SkillEntry or None
        """
        for entry in self.skills.values():
            if entry.metadata and entry.metadata.skill_key == skill_key:
                return entry
            if entry.skill.name == skill_key:
                return entry
        return None

    def get_skill_by_resource_id(self, resource_id: str) -> Optional[SkillEntry]:
        """Resolve a skill by its stable ``{source}:{name}`` resource id.

        A custom skill shadows a builtin of the same name, but they remain
        distinct authorization objects; this resolver returns exactly the source
        requested. Returns None when no entry matches (or the id is malformed).
        """
        if not resource_id or ":" not in resource_id:
            return None
        source, _, name = resource_id.partition(":")
        for entry in self.skills.values():
            sk = entry.skill
            entry_source = self.skills_config.get(sk.name, {}).get("source") or sk.source
            if sk.name == name and entry_source == source:
                return entry
            # Fall back to the loaded source when config is not yet synced.
            if sk.name == name and sk.source == source:
                return entry
            # The definition this entry shadowed is still its own resource: a
            # name collision no longer drops it from the registry, and a caller
            # that answers an ambiguous name with the *other* id must reach the
            # source it named rather than be served the winner's file.
            shadow = entry.shadowed
            if (shadow is not None and shadow.skill.name == name
                    and shadow.skill.source == source):
                return shadow
        return None

    def resolve_skill(self, *, resource_id: Optional[str] = None,
                      name: Optional[str] = None) -> Optional[SkillEntry]:
        """Resolve a skill uniquely by resource_id (preferred) or compatible name.

        ``name`` is only accepted when it maps to exactly one loaded skill in the
        current tenant/scope; an ambiguity (builtin + custom same name) is
        reported by raising ``SkillNameAmbiguous`` so the caller can demand
        ``resource_id`` — and so answer 400 instead of doing the write the name
        happened to pick.
        """
        if resource_id:
            return self.get_skill_by_resource_id(resource_id)
        if name:
            matches = [e for e in self.skills.values() if e.skill.name == name]
            if len(matches) > 1:
                raise SkillNameAmbiguous(name, [e.skill.source for e in matches])
            entry = matches[0] if matches else None
            if entry is not None:
                self.require_unique_name(entry)
            return entry
        return None

    def require_unique_name(self, entry: SkillEntry) -> None:
        """Raise when ``entry``'s name also names a *different* definition.

        :raises SkillNameAmbiguous: when a bare ``name`` cannot identify one
            definition. Callers that hold a ``resource_id`` never get here.
        """
        sources = self.ambiguous_sources(entry)
        if sources:
            raise SkillNameAmbiguous(entry.skill.name,
                                     [entry.skill.source] + sources)

    def ambiguous_sources(self, entry: SkillEntry) -> List[str]:
        """Sources of same-name definitions ``entry``'s bare name could mean.

        Empty means the name is unique and safe to resolve. A shadowed entry
        that *is* the installation's own copy is not a second definition —
        startup copies every builtin skill directory into the workspace
        (``_sync_builtin_skills`` in ``app.py``) and replaces it on the next
        start, which is the same skill by a workspace path. Only a workspace
        definition the installation does not ship is a distinct resource, and
        only that makes the name ambiguous.
        """
        shadow = entry.shadowed
        if shadow is None:
            return []
        if self.ships_with_install(entry.skill):
            return []
        return [shadow.skill.source]

    def ships_with_install(self, skill: Skill) -> bool:
        """True when this skill's files come back from the installation.

        Not just ``source == "builtin"``: startup copies every builtin skill
        directory into the workspace and deletes whatever was there first
        (``_sync_builtin_skills``), so the copy the loader resolves is a
        ``custom`` one that is *still* replaced on the next start. Answering
        this in one place is what keeps the read/write refusal
        (:meth:`SkillService._ships_with_install`) and the name-ambiguity
        decision from disagreeing about the same pair of entries.
        """
        if skill.source == "builtin":
            return True
        try:
            shadowed = os.path.join(self.builtin_dir, os.path.basename(skill.base_dir))
        except Exception:
            return False
        return os.path.isfile(os.path.join(shadowed, "SKILL.md"))
