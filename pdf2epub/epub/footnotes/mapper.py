"""
Footnote mapping functionality for building occurrence-based mappings.
"""

from typing import Dict, List, Set, Tuple
from loguru import logger

from .content_index import ContentAddressIndex
from .models import FootnoteDefinition


class FootnoteMapper:
    """
    Builds various mappings between footnote references and definitions.
    """

    def __init__(self, content_index: ContentAddressIndex):
        self.content_index = content_index

        # For occurrence-based mapping in GLOBAL mode
        self.reference_occurrence_count: Dict[Tuple[str, str], int] = {}  # (key, chapter) -> occurrence number
        self.reference_occurrence_in_file: Dict[Tuple[str, str, int], int] = {}  # (key, chapter, occurrence_in_file) -> occurrence number
        self.definition_by_occurrence: Dict[Tuple[str, int], FootnoteDefinition] = {}  # (key, occurrence_num) -> definition
        self.definition_occurrence_in_file: Dict[Tuple[str, str, int], int] = {}  # (key, chapter, occurrence_in_file) -> occurrence number

        # For LOCAL mode with multi-part chapters
        self.local_chapter_groups: Dict[str, List[str]] = {}  # base_chapter -> list of part files
        self.local_part_to_group: Dict[str, str] = {}  # part file -> logical local footnote group
        self.local_chapter_groups_by_key: Dict[str, Dict[str, List[str]]] = {}
        self.local_part_to_group_by_key: Dict[str, Dict[str, str]] = {}
        self.local_occurrence_mapping: Dict[Tuple[str, str], Dict] = {}  # (key, base_chapter) -> mappings


    def build_chapter_groups(
        self,
        references: Dict[str, List],
        definitions: Dict[str, List[FootnoteDefinition]],
    ) -> None:
        """Build local scopes from the authoritative content address index."""
        self.local_chapter_groups_by_key = {}
        self.local_part_to_group_by_key = {}
        (
            self.local_chapter_groups,
            self.local_part_to_group,
        ) = self.content_index.build_local_groups(references, definitions)
        for key in set(references) | set(definitions):
            groups, part_to_group = self.content_index.build_local_groups(
                {key: references.get(key, [])},
                {key: definitions.get(key, [])},
            )
            self.local_chapter_groups_by_key[key] = groups
            self.local_part_to_group_by_key[key] = part_to_group
        logger.debug(
            f"Built {len(self.local_chapter_groups)} structural chapter groups "
            f"and key-specific scopes for {len(self.local_chapter_groups_by_key)} keys"
        )

    def get_local_group_for_part(self, part_name: str, key: str = "") -> str:
        """Return a key-specific logical scope, or the compatibility scope."""
        if key:
            return self.local_part_to_group_by_key.get(key, {}).get(part_name, part_name)
        return self.local_part_to_group.get(part_name, part_name)

    def get_local_chapter_group(self, group_id: str, key: str = "") -> List[str]:
        """Return ordered source parts for a key-specific or legacy scope."""
        if key:
            return self.local_chapter_groups_by_key.get(key, {}).get(group_id, [])
        return self.local_chapter_groups.get(group_id, [])

    def build_occurrence_mapping(
        self,
        references: Dict[str, List],
        definitions: Dict[str, List[FootnoteDefinition]],
        primary_definition_chapters: Set[str],
        force_global: bool,
        auto_global: bool
    ) -> None:
        """
        Build a mapping from references to definitions based on occurrence order.

        Args:
            references: Dictionary of key -> list of FootnoteReference
            definitions: Dictionary of key -> list of FootnoteDefinition
            primary_definition_chapters: Set of primary definition chapter names
            force_global: If True, force global style
            auto_global: If True, auto-detected global mode
        """
        self.reference_occurrence_count = {}
        self.reference_occurrence_in_file = {}
        self.definition_by_occurrence = {}
        self.definition_occurrence_in_file = {}

        # Preserve every definition. In auto-global mode, notes chapters add a
        # cross-file linking capability; they do not invalidate chapter-local
        # definitions or turn same-numbered notes into one global sequence.
        all_defs = [
            (key, defn)
            for key, def_list in definitions.items()
            for defn in def_list
        ]
        all_defs.sort(key=lambda item: (self._chapter_sort_key(item[1].chapter), item[1].line_num))

        def_scope = {
            id(defn): self.get_local_group_for_part(defn.chapter, key)
            for key, defn in all_defs
        }
        definition_occurrence_by_id: Dict[int, int] = {}

        # Sort all references by chapter and line number
        all_refs = []
        for key, ref_list in references.items():
            for ref in ref_list:
                all_refs.append((key, ref.chapter, ref.line_num))
        all_refs.sort(key=lambda x: (self._chapter_sort_key(x[1]), x[2]))

        # Count occurrences of each key in references
        ref_counts = {}
        occurrence_in_file = {}
        for key, chapter, line_num in all_refs:
            count = ref_counts.get(key, 0) + 1
            ref_counts[key] = count
            # Store the occurrence number for this specific reference
            self.reference_occurrence_count[(key, chapter)] = count
            file_counter_key = (key, chapter)
            occurrence_in_file[file_counter_key] = occurrence_in_file.get(file_counter_key, 0) + 1
            self.reference_occurrence_in_file[(key, chapter, occurrence_in_file[file_counter_key])] = count

        # Assign stable global occurrence ids to all definitions for HTML ids.
        def_counts = {}
        definition_in_file = {}
        for key, defn in all_defs:
            count = def_counts.get(key, 0) + 1
            def_counts[key] = count
            self.definition_by_occurrence[(key, count)] = defn
            definition_occurrence_by_id[id(defn)] = count
            file_counter_key = (key, defn.chapter)
            definition_in_file[file_counter_key] = definition_in_file.get(file_counter_key, 0) + 1
            self.definition_occurrence_in_file[
                (key, defn.chapter, definition_in_file[file_counter_key])
            ] = count

        definitions_by_key: Dict[str, List[FootnoteDefinition]] = {}
        for key, defn in all_defs:
            definitions_by_key.setdefault(key, []).append(defn)

        # Link each structural local scope first. References with no local
        # definition may use definitions outside their scope (the centralized
        # endnotes case), but never borrow another chapter's local definition
        # merely because its key matches.
        refs_by_key: Dict[str, List] = {}
        for key, ref_list in references.items():
            refs_by_key[key] = sorted(
                ref_list,
                key=lambda item: (self._chapter_sort_key(item.chapter), item.line_num),
            )
        for key, refs_for_key in refs_by_key.items():
            defs_for_key = definitions_by_key.get(key, [])
            by_scope: Dict[str, List[FootnoteDefinition]] = {}
            for defn in defs_for_key:
                by_scope.setdefault(def_scope[id(defn)], []).append(defn)
            refs_by_scope: Dict[str, List] = {}
            for ref in refs_for_key:
                refs_by_scope.setdefault(self.get_local_group_for_part(ref.chapter, key), []).append(ref)
            # Notes-unit definitions are cross-scope candidates only when their
            # own structural group has no references for this key. If a Notes
            # child was grouped with its owning chapter, its matching local
            # references keep it local regardless of TOC depth.
            global_defs = []
            if primary_definition_chapters:
                global_defs = [
                    defn for defn in defs_for_key
                    if defn.chapter in primary_definition_chapters
                    and not refs_by_scope.get(def_scope[id(defn)], [])
                ]
            if not primary_definition_chapters:
                # Legacy global sequences may use definitions in a dedicated
                # definition-only scope, but an owning scope with references
                # is always local even when no Notes role was supplied.
                global_defs = [
                    defn for defn in defs_for_key
                    if not refs_by_scope.get(def_scope[id(defn)], [])
                ]

            global_refs: List = []
            scope_ref_positions: Dict[str, int] = {}
            file_ref_positions: Dict[str, int] = {}
            for ref in refs_for_key:
                scope = self.get_local_group_for_part(ref.chapter, key)
                file_ref_positions[ref.chapter] = file_ref_positions.get(ref.chapter, 0) + 1
                file_occ = file_ref_positions[ref.chapter]
                scope_defs = by_scope.get(scope, [])
                if scope_defs:
                    scope_ref_positions[scope] = scope_ref_positions.get(scope, 0) + 1
                    local_ref_occ = scope_ref_positions[scope]
                    if len(scope_defs) == 1:
                        target = definition_occurrence_by_id[id(scope_defs[0])]
                    elif local_ref_occ <= len(scope_defs):
                        target = definition_occurrence_by_id[id(scope_defs[local_ref_occ - 1])]
                    else:
                        target = 0
                    self.reference_occurrence_count[(key, ref.chapter)] = target
                    self.reference_occurrence_in_file[(key, ref.chapter, file_occ)] = target
                else:
                    global_refs.append(ref)

            # Centralized definitions are paired in order only with references
            # outside scopes that have their own definition.
            per_file: Dict[str, int] = {}
            global_targets = [definition_occurrence_by_id[id(defn)] for defn in global_defs]
            for ref_index, ref in enumerate(global_refs):
                per_file[ref.chapter] = per_file.get(ref.chapter, 0) + 1
                file_occ = per_file[ref.chapter]
                occurrence = (
                    global_targets[0] if len(global_targets) == 1
                    else global_targets[ref_index] if ref_index < len(global_targets)
                    else 0
                )
                self.reference_occurrence_in_file[(key, ref.chapter, file_occ)] = occurrence
                self.reference_occurrence_count[(key, ref.chapter)] = occurrence

        logger.debug(f"Built occurrence mapping: {len(ref_counts)} unique ref keys, {len(def_counts)} unique def keys")

    def build_local_occurrence_mappings(
        self,
        references: Dict[str, List],
        definitions: Dict[str, List[FootnoteDefinition]],
        chapter_definitions: Dict[str, Set[str]],
        chapter_references: Dict[str, Set[str]]
    ) -> None:
        """
        Build occurrence mappings for LOCAL mode with multi-part chapters.

        Args:
            references: Dictionary of key -> list of FootnoteReference
            definitions: Dictionary of key -> list of FootnoteDefinition
            chapter_definitions: Dictionary of chapter -> set of defined keys
            chapter_references: Dictionary of chapter -> set of referenced keys
        """
        self.local_occurrence_mapping = {}
        for key, groups in self.local_chapter_groups_by_key.items():
            for base_chapter, part_files in groups.items():
                if len(part_files) <= 1:
                    continue
                refs = sorted(
                    (ref for ref in references.get(key, []) if ref.chapter in part_files),
                    key=lambda ref: (self._chapter_sort_key(ref.chapter), ref.line_num),
                )
                defs = sorted(
                    (defn for defn in definitions.get(key, []) if defn.chapter in part_files),
                    key=lambda defn: (self._chapter_sort_key(defn.chapter), defn.line_num),
                )
                mapping = {
                    "reference_positions": {},
                    "reference_occurrence_in_file": {},
                    "definition_positions": {},
                    "definition_occurrence_in_file": {},
                    "reference_occurrence_count": {},
                    "definition_by_occurrence": {},
                }
                refs_in_file: Dict[str, int] = {}
                single_definition = len(defs) == 1
                for position, ref in enumerate(refs, 1):
                    refs_in_file[ref.chapter] = refs_in_file.get(ref.chapter, 0) + 1
                    target_position = 1 if single_definition else position
                    mapping["reference_positions"][(key, ref.chapter, ref.line_num)] = target_position
                    mapping["reference_occurrence_in_file"][(key, ref.chapter, refs_in_file[ref.chapter])] = target_position
                    mapping["reference_occurrence_count"][(key, ref.chapter)] = target_position

                defs_in_file: Dict[str, int] = {}
                for position, defn in enumerate(defs, 1):
                    defs_in_file[defn.chapter] = defs_in_file.get(defn.chapter, 0) + 1
                    mapping["definition_positions"][(key, defn.chapter, defn.line_num)] = position
                    mapping["definition_occurrence_in_file"][(key, defn.chapter, defs_in_file[defn.chapter])] = position
                    mapping["definition_by_occurrence"][(key, position)] = defn

                if not single_definition and len(refs) != len(defs):
                    logger.warning(
                        f"Footnote count mismatch in {base_chapter} for [{key}]: "
                        f"{len(refs)} references, {len(defs)} definitions"
                    )

                self.local_occurrence_mapping[(key, base_chapter)] = mapping

    def _chapter_sort_key(self, chapter_name: str) -> tuple:
        """
        Generate a sort key for chapter names to maintain proper order.

        Args:
            chapter_name: The chapter name to sort

        Returns:
            Tuple for sorting
        """
        return self.content_index.order_key(chapter_name)
