"""
This was copied from MemoryAlpha and will need integration.
Most of the code is self-sufficient here so the only major thing might be the
embed creation aspect?

"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import discord
import pssapi
from fuzzywuzzy import fuzz
from icecream import ic

if TYPE_CHECKING:
    from classes import FleetToolsBot

# Constants
MAX_RECURSION_DEPTH = 10 # Stop recursion explosion
MAX_PATHS_PER_TARGET = 100  # Limit paths explored per target to prevent explosion
MAX_SUB_PATHS_TO_COMBINE = 50  # Limit sub-path combinations to prevent cartesian explosion
MAX_TOTAL_PATHS = 400  # Stop searching after finding this many total paths

_current_prestige_graph: Optional['PrestigeGraph'] = None

def set_current_prestige_graph(graph: 'PrestigeGraph'):
    import time
    start_time = time.time()

    global _current_prestige_graph
    _current_prestige_graph = graph
    # Clear the cache when setting a new graph
    _find_craft_paths_for_crew.cache_clear()

    elapsed = time.time() - start_time
    print(f"Function 'set_current_prestige_graph' took {elapsed:.2f} seconds to run.")

@dataclass
class CrewMember:
    name: str = ""
    crew_id: str = ""
    design_id: str = ""
    rarity: str = ""
    equipmask: int = 0
    special: str = ""
    collection: str = ""
    hp: float = 0
    atk: float = 0
    rpr: float = 0
    abl: float = 0
    sta: float = 0
    plt: float = 0
    sci: float = 0
    eng: float = 0
    wpn: float = 0
    rst: float = 0
    walk: int = 0
    run: int = 0
    tp: int = 0

    def __repr__(self):
        return f"CrewMember(name='{self.name}', design_id='{self.design_id}', rarity='{self.rarity}')"
    def __str__(self):
        return f"{self.name}"
    def as_tuple(self):
        return self.name, self.crew_id, self.design_id

@dataclass
class PrestigeRecipe:
    crew1_name: str
    crew1_id: int
    crew1_rarity: str
    crew2_name: str
    crew2_id: int
    crew2_rarity: str
    result_name: str
    result_id: int
    result_rarity: str

    def __repr__(self):
        return f"PrestigeRecipe({self.crew1_name}, {self.crew2_name} -> {self.result_name})"
    def __str__(self):
        return f"{self.result_name}"
    def as_tuple(self):
        return self.crew1_name, self.crew2_name, self.result_name
    def to_dict(self):
        return {
            "crew1_name": self.crew1_name,
            "crew1_id": self.crew1_id,
            "crew1_rarity": self.crew1_rarity,
            "crew2_name": self.crew2_name,
            "crew2_id": self.crew2_id,
            "crew2_rarity": self.crew2_rarity,
            "result_name": self.result_name,
            "result_id": self.result_id,
            "result_rarity": self.result_rarity
        }

    @staticmethod
    def from_dict(data: dict) -> "PrestigeRecipe":
        return PrestigeRecipe(
            crew1_name=data["crew1_name"],
            crew1_id=data["crew1_id"],
            crew1_rarity=data["crew1_rarity"],
            crew2_name=data["crew2_name"],
            crew2_id=data["crew2_id"],
            crew2_rarity=data["crew2_rarity"],
            result_name=data["result_name"],
            result_id=data["result_id"],
            result_rarity=data["result_rarity"]
        )

    def __eq__(self, other):
        if isinstance(other, PrestigeRecipe):
            return self.result_id == other.result_id
        elif isinstance(other, (str, int)):
            return self.result_id == other or self.result_name == other
        return False
    def __ne__(self, other):
        return not self.__eq__(other)

@dataclass
class PrestigePath:
    steps: List[PrestigeRecipe] = field(default_factory=list)
    required_crew: Dict[int, str] = field(default_factory=dict)  # design_id -> crew_name
    intermediate_crafts: List[int] = field(default_factory=list)  # design_ids of intermediate crews needed

    def add_step(self, recipe: PrestigeRecipe):
        self.steps.append(recipe)

    def get_display_string(self) -> str:
        lines = []
        for i, recipe in enumerate(self.steps, 1):
            lines.append(f"**Step {i}:** {recipe.crew1_name} + {recipe.crew2_name} = {recipe.result_name}")
        return "\n".join(lines) if lines else "No prestige path found"

@dataclass
class PrestigeGraph:
    graph: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # target_name -> [(crew1_name, crew2_name), ...]
    crew_lookup: Dict[str, int] = field(default_factory=dict)  # crew_name -> design_id
    id_to_name: Dict[int, str] = field(default_factory=dict)  # design_id -> crew_name
    name_lookup_lower: Dict[str, str] = field(default_factory=dict)  # lowercase_name -> actual_name (for fast case-insensitive lookup)

    
    def get_recipes_for_target(self, target_name: str) -> List[Tuple[str, str]]:
        target_name_lower = target_name.lower()
        actual_target_name = self.name_lookup_lower.get(target_name_lower)
        if actual_target_name:
            return self.graph.get(actual_target_name, [])
        return []


async def generate_crewmember_list_from_raw(raw_crew_list: pssapi.entities.character.Character):
    crew_list: dict[str, CrewMember] = {}

    for crew in raw_crew_list["Character"]:
        crew_id = crew.get("CharacterId", "Unknown")
        crew_name = crew.get("CharacterName", "Unknown")
        design_id = crew.get("CharacterDesignId", "Unknown")

        crew_list[crew_id] = CrewMember(
            name=crew_name,
            crew_id=crew_id,
            design_id=design_id,
        )
    return crew_list


async def compile_prestige_graph(prestige_recipes: Dict[int, List[PrestigeRecipe]]) -> PrestigeGraph:
    graph = defaultdict(list)
    crew_lookup = {}  # crew_name -> design_id
    id_to_name = {}  # design_id -> crew_name
    name_lookup_lower = {}  # lowercase_name -> actual_name

    recipe_count = 0
    for result_id, recipes in prestige_recipes.items():
        for recipe in recipes:
            target_name = recipe.result_name

            # Build lookup tables
            crew_lookup[recipe.crew1_name] = recipe.crew1_id
            crew_lookup[recipe.crew2_name] = recipe.crew2_id
            crew_lookup[recipe.result_name] = recipe.result_id
            id_to_name[recipe.crew1_id] = recipe.crew1_name
            id_to_name[recipe.crew2_id] = recipe.crew2_name
            id_to_name[recipe.result_id] = recipe.result_name

            # Build lowercase lookup cache for O(1) case-insensitive search
            name_lookup_lower[recipe.crew1_name.lower()] = recipe.crew1_name
            name_lookup_lower[recipe.crew2_name.lower()] = recipe.crew2_name
            name_lookup_lower[recipe.result_name.lower()] = recipe.result_name

            # Create sorted source names tuple for consistency
            sorted_source_names = tuple(sorted([recipe.crew1_name, recipe.crew2_name]))

            # Add to graph if not already present (deduplicate)
            if sorted_source_names not in graph[target_name]:
                graph[target_name] = graph[target_name] or []
                graph[target_name].append(sorted_source_names)

            # Yield to event loop every 50 recipes to prevent blocking heartbeat
            recipe_count += 1
            if recipe_count % 50 == 0:
                await asyncio.sleep(0)

    # Sort the graph entries (yield periodically during sorting)
    sorted_items = sorted(graph.items(), key=lambda x: x[0].lower())
    sorted_graph = {}

    for i, (k, v) in enumerate(sorted_items):
        sorted_graph[k] = sorted(v, key=lambda x: (x[0].lower(), x[1].lower()))
        # Yield every 25 items during sorting
        if i % 25 == 0:
            await asyncio.sleep(0)

    return PrestigeGraph(
        graph=sorted_graph,
        crew_lookup=crew_lookup,
        id_to_name=id_to_name,
        name_lookup_lower=name_lookup_lower
    )


async def build_prestige_recipes(
        bot: "FleetToolsBot",
        progress_callback=None
) -> Dict[int, List[PrestigeRecipe]]:
    prestige_recipes = {}
    seen_recipes = set()  # Track (result_id, min(crew1_id, crew2_id), max(crew1_id, crew2_id)) to deduplicate

    # Get all crew from API cache
    all_crew = bot.cache_manager.api_crew_list
    if not all_crew:
        return prestige_recipes

    # Create a lookup dict for crew by design_id
    crew_lookup = {crew.design_id: crew for crew in all_crew}

    total_crew = len(all_crew)
    recipes_count = 0
    processed_crew = 0

    for crew_index, crew in enumerate(all_crew):
        if crew.rarity in ["Special", "Legendary", "Common", "Elite"]:
            continue
        processed_crew += 1
        prestige_crews = await bot.api_manager.prestige_from(crew.design_id)
        if not prestige_crews:
            continue
        for prestige_crew in prestige_crews:
            # Get crew information from the prestige response and lookup
            crew1_id = prestige_crew.character_design_id_1
            crew2_id = prestige_crew.character_design_id_2
            result_id = prestige_crew.to_character_design_id

            crew1_info = crew_lookup.get(crew1_id)
            crew2_info = crew_lookup.get(crew2_id)
            result_info = crew_lookup.get(result_id)

            # Skip if we can't find all crew info
            if not (crew1_info and crew2_info and result_info):
                continue

            # Create a deduplication key: (result_id, min(crew1_id, crew2_id), max(crew1_id, crew2_id))
            # This treats Crew1+Crew2=Result and Crew2+Crew1=Result as duplicates
            crew_pair = (min(crew1_id, crew2_id), max(crew1_id, crew2_id))
            dedup_key = (result_id, crew_pair[0], crew_pair[1])

            # Skip if we've already seen this recipe combination
            if dedup_key in seen_recipes:
                continue

            seen_recipes.add(dedup_key)

            # Create the recipe with all required information
            recipe = PrestigeRecipe(
                crew1_name=crew1_info.name,
                crew1_id=crew1_id,
                crew1_rarity=crew1_info.rarity,
                crew2_name=crew2_info.name,
                crew2_id=crew2_id,
                crew2_rarity=crew2_info.rarity,
                result_name=result_info.name,
                result_id=result_id,
                result_rarity=result_info.rarity
            )

            # Store by result_id, keying multiple recipes per result
            if result_id not in prestige_recipes:
                prestige_recipes[result_id] = []
            prestige_recipes[result_id].append(recipe)
            recipes_count += 1

        # Report progress to console
        if processed_crew % 10 == 0:
            print(f"[Prestige Recipes] Processed {processed_crew} crew, {recipes_count} recipes found")

        # Yield to event loop periodically
        if crew_index % 10 == 0:
            await asyncio.sleep(0)

    print(f"[Prestige Recipes] ✅ Complete: {processed_crew} crew processed, {recipes_count} unique recipes found")
    return prestige_recipes


async def load_prestige_recipes_from_storage(bot: "FleetToolsBot") -> Dict[int, List[PrestigeRecipe]]:
    try:
        stored_data = bot.cache_manager.load_prestige_recipes()

        if not stored_data:
            ic("No prestige recipes in storage - will rebuild")
            return {}

        prestige_recipes = {}
        for result_id, recipes_list in stored_data.items():
            # result_id is already an int from data_manager.load_prestige_recipes()
            # recipes_list should be a list of dicts
            if not isinstance(recipes_list, list):
                continue
            # Convert from dict format back to PrestigeRecipe objects
            prestige_recipes[result_id] = [
                PrestigeRecipe.from_dict(recipe_dict) for recipe_dict in recipes_list
            ]

        return prestige_recipes
    except Exception as e:
        ic(f"Error loading prestige recipes from storage: {e}")
        return {}

def filter_crew_by_minimum_rarity(
        bot: "FleetToolsBot",
        player_crew: Dict[str, CrewMember],
        min_rarity: str) -> Dict[str, CrewMember]:
    # Define rarity hierarchy
    rarity_order = {
        "Common": 1,
        "Elite": 2,
        "Unique": 3,
        "Epic": 4,
        "Hero": 5,
        "Special": 6,
        "Legendary": 7
    }

    # If min_rarity is not in our order or is "Common", return all crew
    if min_rarity not in rarity_order or min_rarity == "Common":
        return player_crew

    min_rarity_value = rarity_order[min_rarity]

    # Get the API crew list and create a lookup dictionary by design_id
    cached_crew_list = bot.cache_manager.get_api_crew_list()
    if not cached_crew_list:
        # If cache not loaded, return all crew (fail-safe)
        bot.logger.warning("API crew list not loaded, cannot filter by rarity")
        return player_crew

    # Create design_id -> CrewMember lookup
    design_id_lookup = {str(crew.design_id): crew for crew in cached_crew_list}

    return { # oh god i hope this works
        crew_id: crew_member
        for crew_id, crew_member in player_crew.items()
        if (lambda rarity: rarity in rarity_order and rarity_order[rarity] >= min_rarity_value)(
            (design_id_lookup.get(str(crew_member.design_id)) or crew_member).rarity
        )
    }

async def resolve_excluded_crew(
        exclude_str: Optional[str],
        player_crew: Dict[str, CrewMember],
        bot: "FleetToolsBot") -> Tuple[Dict[str, CrewMember], List[str]]:
    if not exclude_str or not exclude_str.strip():
        return player_crew, []

    # Parse the exclusion string
    exclude_names = [name.strip() for name in exclude_str.split(",") if name.strip()]
    modified_crew = dict(player_crew) # Create a new dict copy
    matched_exclusions = []

    for exclude_name in exclude_names:
        best_match_crew_id = None
        best_match_crew = None
        best_score = 0
        threshold = 80

        exclude_name_lower = exclude_name.lower()

        # Find best fuzzy match in player's crew by crew name
        for crew_id, crew_member in modified_crew.items():
            crew_name_lower = crew_member.name.lower()
            score = fuzz.token_set_ratio(exclude_name_lower, crew_name_lower)

            if score > best_score:
                best_score = score
                best_match_crew_id = crew_id
                best_match_crew = crew_member

        # If match found with sufficient score, remove one instance
        if best_score >= threshold and best_match_crew_id is not None:
            del modified_crew[best_match_crew_id]
            matched_exclusions.append(best_match_crew.name)

    return modified_crew, matched_exclusions

def _subtract_path_consumption(owned_crew: Dict[str, int], path: Tuple[str, ...]) -> Dict[str, int]:
    import time
    start_time = time.time()

    result = owned_crew.copy()
    for step in path:
        if " = " in step:
            sources_part = step.split(" = ")[0]
            crews = sources_part.split(" + ")
            for crew in crews:
                crew = crew.strip()
                # Match by base name (case-insensitive)
                crew_base = crew.split("(")[0].strip().lower()
                for key in list(result.keys()):
                    key_base = key.split("(")[0].strip().lower()
                    if key_base == crew_base and result[key] > 0:
                        result[key] -= 1
                        break

    elapsed = time.time() - start_time
    if elapsed > 0.01:  # Only log if takes more than 10ms
        print(f"Function '_subtract_path_consumption' took {elapsed:.2f} seconds to run.")

    return result

@cache
def _find_craft_paths_for_crew(
        target_name: str,
        owned_crew_tuple: Tuple[Tuple[str, int], ...],
        depth: int = 0
) -> List[Tuple[str, ...]]:
    """
    Find all ways to craft target_name from the given owned crew.
    Returns a list of step-tuples like ("CrewA + CrewB = Target",).

    NOTE: current_path is intentionally NOT a cache key - whether a crew
    can be crafted depends only on what you own, not how you got here.
    """
    import time
    start_time = time.time()

    global _current_prestige_graph
    if _current_prestige_graph is None or depth > MAX_RECURSION_DEPTH:
        return []

    owned_crew = dict(owned_crew_tuple)
    all_paths: List[Tuple[str, ...]] = []

    target_name_lower = target_name.lower()
    actual_target_name = _current_prestige_graph.name_lookup_lower.get(target_name_lower, target_name)

    if actual_target_name not in _current_prestige_graph.graph:
        return []

    recipe_sources = _current_prestige_graph.graph.get(actual_target_name, [])
    limited_recipes = recipe_sources[:MAX_PATHS_PER_TARGET]

    for sources in limited_recipes:
        if len(all_paths) >= MAX_TOTAL_PATHS:
            break
        _find_paths_with_sources(sources, actual_target_name, owned_crew.copy(), depth, all_paths)

    unique_paths = list(set(all_paths))
    unique_paths.sort(key=len)

    elapsed = time.time() - start_time
    if elapsed > 0.1:
        print(f"Function '_find_craft_paths_for_crew' for '{target_name}' (depth={depth}) took {elapsed:.2f}s, found {len(unique_paths)} paths")

    return unique_paths[:MAX_TOTAL_PATHS]


def _get_source_options(
        source: str,
        owned_crew: Dict[str, int],
        depth: int
) -> List[Tuple[str, List[Tuple[str, ...]], Dict[str, int]]]:
    import time
    start_time = time.time()

    global _current_prestige_graph
    if _current_prestige_graph is None:
        return []

    base_name = source.split("(")[0].strip()
    options = []

    # Check if we own this crew directly
    for crew_name, count in owned_crew.items():
        crew_base_name = crew_name.split("(")[0].strip()
        if base_name.lower() == crew_base_name.lower() and count > 0:
            remaining = owned_crew.copy()
            remaining[crew_name] -= 1
            options.append((crew_name, [], remaining))
            break  # Only one way to consume a directly-owned crew

    # If we don't own it, try to craft it
    if not options:
        sub_paths = _find_craft_paths_for_crew(
            source,
            tuple(owned_crew.items()),
            depth + 1
        )
        if sub_paths:
            for sub_path in sub_paths[:MAX_SUB_PATHS_TO_COMBINE]:
                remaining = _subtract_path_consumption(owned_crew, sub_path)
                options.append((source, [sub_path], remaining))

    elapsed = time.time() - start_time
    if elapsed > 0.05:
        print(f"Function '_get_source_options' for '{source}' took {elapsed:.2f} seconds to run.")

    return options

def _find_paths_with_sources(
        sources: Tuple[str, str],
        target_name: str,
        owned_crew: Dict[str, int],
        depth: int,
        all_paths: List[Tuple[str, ...]]
):
    import time
    start_time = time.time()

    if len(all_paths) >= MAX_TOTAL_PATHS:
        return

    source1 = sources[0]
    source2 = sources[1]

    source1_options = _get_source_options(source1, owned_crew, depth)
    if not source1_options:
        return

    source1_options = source1_options[:MAX_SUB_PATHS_TO_COMBINE]

    for crew_key1, sub_paths1, remaining_crew1 in source1_options:
        if len(all_paths) >= MAX_TOTAL_PATHS:
            return

        source2_options = _get_source_options(source2, remaining_crew1, depth)
        if not source2_options:
            continue

        source2_options = source2_options[:MAX_SUB_PATHS_TO_COMBINE]

        for crew_key2, sub_paths2, _remaining_crew2 in source2_options:
            if len(all_paths) >= MAX_TOTAL_PATHS:
                return

            step = f"{crew_key1} + {crew_key2} = {target_name}"

            if not sub_paths1 and not sub_paths2:
                all_paths.append((step,))
            elif sub_paths1 and not sub_paths2:
                for prereq in sub_paths1[:MAX_SUB_PATHS_TO_COMBINE]:
                    all_paths.append(prereq + (step,))
                    if len(all_paths) >= MAX_TOTAL_PATHS:
                        return
            elif not sub_paths1 and sub_paths2:
                for prereq in sub_paths2[:MAX_SUB_PATHS_TO_COMBINE]:
                    all_paths.append(prereq + (step,))
                    if len(all_paths) >= MAX_TOTAL_PATHS:
                        return
            else:
                limited_paths1 = sub_paths1[:MAX_SUB_PATHS_TO_COMBINE]
                limited_paths2 = sub_paths2[:MAX_SUB_PATHS_TO_COMBINE]
                for prereq1 in limited_paths1:
                    for prereq2 in limited_paths2:
                        all_paths.append(prereq1 + prereq2 + (step,))
                        if len(all_paths) >= MAX_TOTAL_PATHS:
                            return

    elapsed = time.time() - start_time
    if elapsed > 0.1:
        print(f"Function '_find_paths_with_sources' for '{target_name}' took {elapsed:.2f} seconds to run.")


# Keep old name as alias so _run_pathfinding_sync and cache_clear() still work
def _find_all_paths_for_crew_internal(
        target_name: str,
        owned_crew_tuple: Tuple[Tuple[str, int], ...],
        current_path: Tuple[str, ...] = (),
        depth: int = 0
) -> List[Tuple[str, ...]]:
    return _find_craft_paths_for_crew(target_name, owned_crew_tuple, depth)

def _run_pathfinding_sync(target_crew_name: str, owned_crew_tuple: Tuple[Tuple[str, int], ...]) -> List[Tuple[str, ...]]:
    import time
    start_time = time.time()

    result = _find_all_paths_for_crew_internal(target_crew_name, owned_crew_tuple, (), 0)

    elapsed = time.time() - start_time
    print(f"Function '_run_pathfinding_sync' for '{target_crew_name}' took {elapsed:.2f}s, found {len(result)} paths")

    return result


async def find_prestige_paths(
        bot: "FleetToolsBot",
        player_crew: Dict[str, CrewMember],
        target_crew_id: int,
        prestige_recipes: Dict[int, List[PrestigeRecipe]],
        excluded_crew: Optional[List[str]] = None,
        max_depth: int = 5,
        return_limit: int = 20  # Maximum paths to return
) -> Tuple[List[PrestigePath], Optional[str]]:
    # Compile the prestige graph from recipes (async to prevent heartbeat blocking)
    prestige_graph = await compile_prestige_graph(prestige_recipes)

    # Yield to event loop after compilation
    await asyncio.sleep(0)

    # Set the global prestige graph for the recursive functions
    set_current_prestige_graph(prestige_graph)

    # Get target crew name from ID
    target_crew_name = prestige_graph.id_to_name.get(target_crew_id)
    if not target_crew_name:
        return [], None

    # Build owned crew dictionary with counts.
    # IMPORTANT: Use the canonical CharacterDesignName from the prestige graph (keyed by design_id),
    # NOT crew.name which is the player-facing instance name and may be player-renamed.
    owned_crew_dict: Dict[str, int] = {}
    for crew_member in player_crew.values():
        try:
            design_id_int = int(crew_member.design_id)
            canonical_name = prestige_graph.id_to_name.get(design_id_int)
        except (ValueError, TypeError):
            canonical_name = None

        if canonical_name:
            base_name = canonical_name.split("(")[0].strip()
        else:
            # Fallback: strip level suffix from raw instance name
            base_name = crew_member.name.split("(")[0].strip()

        owned_crew_dict[base_name] = owned_crew_dict.get(base_name, 0) + 1

    # Convert to immutable tuple for caching
    owned_crew_tuple = tuple(owned_crew_dict.items())

    # Run the CPU-intensive pathfinding in a thread executor to prevent blocking heartbeat
    # This allows the event loop to continue processing Discord packets during computation
    loop = asyncio.get_event_loop()
    raw_paths = await loop.run_in_executor(
        None,  # Use default ThreadPoolExecutor
        _run_pathfinding_sync,
        target_crew_name,
        owned_crew_tuple
    )

    # Yield to event loop after pathfinding
    await asyncio.sleep(0)

    if not raw_paths:
        return [], None

    # Convert raw paths (tuples of strings) to PrestigePath objects
    prestige_paths = []

    for path_idx, raw_path in enumerate(raw_paths):
        path_obj = PrestigePath()

        for step in raw_path:
            # Parse step: "Crew1 + Crew2 = Result"
            if " = " not in step:
                continue

            sources_part, result_part = step.split(" = ")
            source_parts = sources_part.split(" + ")

            if len(source_parts) != 2:
                continue

            crew1_name = source_parts[0].strip()
            crew2_name = source_parts[1].strip()
            result_name = result_part.strip()

            # Look up IDs from graph
            crew1_id = prestige_graph.crew_lookup.get(crew1_name)
            crew2_id = prestige_graph.crew_lookup.get(crew2_name)
            result_id = prestige_graph.crew_lookup.get(result_name)

            if not (crew1_id and crew2_id and result_id):
                continue

            # Find the matching recipe to get rarity info
            matching_recipe = None
            if result_id in prestige_recipes:
                for recipe in prestige_recipes[result_id]:
                    if ((recipe.crew1_id == crew1_id and recipe.crew2_id == crew2_id) or
                            (recipe.crew1_id == crew2_id and recipe.crew2_id == crew1_id)):
                        matching_recipe = recipe
                        break

            if matching_recipe:
                path_obj.add_step(matching_recipe)

        if path_obj.steps:
            prestige_paths.append(path_obj)

        # Yield to event loop every 5 paths during conversion
        if path_idx % 5 == 0:
            await asyncio.sleep(0)

    # Sort paths by length (simpler paths first)
    prestige_paths.sort(key=lambda p: len(p.steps))

    # Return up to return_limit paths
    return prestige_paths[:return_limit], None


async def create_prestige_embed(
        player_name: str,
        target_crew,  # Can be str or crew object
        paths: List[PrestigePath],
        missing_crew_status: Optional[str],
        excluded_crew: Optional[List[str]] = None,
        min_rarity: Optional[str] = None,
) -> discord.Embed:
    # Handle both string and object types for target_crew
    if isinstance(target_crew, str):
        target_crew_name = target_crew
    else:
        target_crew_name = getattr(target_crew, 'character_design_name', str(target_crew))

    if not paths and missing_crew_status == "multiple_missing":
        embed = discord.Embed(
            title=f"❌ Multiple Missing Crew",
            description=f"**Player:** {player_name}\n**Target:** {target_crew_name}",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Reason",
            value="Player is missing multiple crew to complete any viable path to prestige.",
            inline=False
        )
        if min_rarity and min_rarity != "Common":
            embed.add_field(
                name="Minimum Rarity Filter",
                value=f"Only using crew of {min_rarity} rarity or higher",
                inline=False
            )
        if excluded_crew:
            embed.add_field(
                name="Excluded Crew",
                value=", ".join(excluded_crew),
                inline=False
            )
        return embed

    if not paths:
        embed = discord.Embed(
            title=f"❌ No Prestige Paths Found",
            description=f"**Player:** {player_name}\n**Target:** {target_crew_name}",
            color=discord.Color.red()
        )
        embed.add_field(
            name="Reason",
            value="The player doesn't have the required crew to prestige into this target.",
            inline=False
        )
        if min_rarity and min_rarity != "Common":
            embed.add_field(
                name="Minimum Rarity Filter",
                value=f"Only using crew of {min_rarity} rarity or higher",
                inline=False
            )
        if excluded_crew:
            embed.add_field(
                name="Excluded Crew",
                value=", ".join(excluded_crew),
                inline=False
            )
        return embed

    embed = discord.Embed(
        title=f"✅ Prestige Paths Found",
        description=f"**Player:** {player_name}\n**Target:** {target_crew_name}",
        color=discord.Color.green()
    )

    # Show each path as a separate field
    for i, path in enumerate(paths, 1):
        embed.add_field(
            name=f"Path {i}",
            value=path.get_display_string() or "Could not calculate path",
            inline=False
        )

    if min_rarity and min_rarity != "Common":
        embed.add_field(
            name="Minimum Rarity Filter",
            value=f"Only using crew of {min_rarity} rarity or higher",
            inline=False
        )
    if excluded_crew:
        embed.add_field(
            name="Excluded Crew",
            value=", ".join(excluded_crew),
            inline=False
        )

    if len(paths) > 20:
        embed.set_footer(text=f"Showing 20 of {len(paths)} possible paths due to maximum length")

    return embed
