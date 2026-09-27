# Voriek design principles

## Player agency

The player chooses an intention and action; the world determines the consequences through its established rules, characters, knowledge, and circumstances. Failure should create a changed path forward, not simply close the story.

The engine must not resolve an irreversible event before the player has a chance to act. When a player gives an explicit stopping point, the scene ends there. Narrative text, persisted state, elapsed time, and location must describe the same moment.

## Checkpoints are events, not cages

A checkpoint represents a story event that has to be addressed by the world, such as an invasion, a deadline, or a relationship rupture. The event may resolve, be prevented, be transformed, or be missed. Its consequences depend on player intervention and world state.

Generated worlds use advisory checkpoint boundaries. They do not invent a barrier simply to force the player back into a checkpoint area. Legacy worlds can retain strict boundaries where needed for compatibility.

## World knowledge and perspective

The world model knows what exists and what happens. A character only knows what they could plausibly perceive, learn, remember, or infer. Rumors are subjective claims with source and confidence; canon facts are world-level truths. The player receives a broader interface through quests, inventory, the map, and the consequence journal without gaining every character's private knowledge.

## Creator authority

A creator is also a player, with additional tools to preview, validate, and commit intentional edits. Creator actions are logged so that later story behavior has an explainable cause. Creator control can override normal constraints, but the UI should make the scope and effect of an override visible.

## Time, travel, and calendars

Each world owns its calendar and opening date. Contemporary settings can use Gregorian time; invented settings can define their own months, seasons, and year labels. AI proposes scene duration while the engine advances the clock deterministically. Travel and explicit time skips have engine-owned duration and destination rules.

The world may warn a player before a proposed time skip crosses a known deadline. A warning does not forbid unusual solutions; it gives the player a chance to choose an informed intervention.

## Genre-neutral state

Items, skills, capabilities, and effects should not assume a particular genre or power hierarchy. An item may be consumable, persistent, or causally significant; its description, conditions, charges, and effects are semantic world data, not a mandatory numeric durability system. Character competence is supported by profile, learned knowledge, and established capability evidence instead of arbitrary genre stats.

## Continuity is visible and recoverable

The consequence journal records what happened and why. Saves, snapshots, and branches preserve a world state that can be revisited without silently rewriting history. Rewinding or continuing a legacy is optional product direction, not an assumption that every story must support.
