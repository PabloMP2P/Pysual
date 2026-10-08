# Extending the control system

Small controls, reusable content, and assembled tools need different
implementation boundaries. A chart legend, a chart export action, and a query
builder should not become unrelated siblings in one growing widget module.

The system has three levels:

1. **Core contracts** own schema, lifetime, tree ownership, input routing,
   layout, events, and painting. Backends implement the host contract rather
   than handling each concrete control type.
2. **Control families and reusable parts** implement related behavior and
   presentation. Parts include press gestures, range rules, editor content,
   scrolling geometry, and explicit style fallbacks.
3. **Composites** assemble ordinary controls with `Container`. They own their
   child names, event handlers, and semantic public properties. The existing
   ownership, disposal, blueprint, and batch-update rules apply to them.

## Source organization

| Location | Responsibility |
| --- | --- |
| `controls.py`, `schema.py` | Base Control/Container contracts, observable properties, atomic updates, and compatibility exports. |
| `_registry.py` | Authoritative registrations, optional legacy aliases, and catalog discovery. |
| `_catalog.py` | One table of built-in types, family names, authoring metadata, and compatibility factory aliases. |
| `_controls/basic.py`, `actions.py` | Labels, buttons, checked actions, toggles, and exclusive choices. |
| `_controls/ranges.py`, `viewports.py` | Bounded values, progress, and scrolling containers. |
| `_controls/lists.py`, `choices.py` | Virtual list rows and choice popup content. |
| `_controls/media.py`, `windows.py` | Image presentation and in-app window lifecycle. |
| `text.py`, `tree.py`, `data_grid.py`, `charts.py`, `menus.py` | Existing focused families; extend or split a family when its responsibilities warrant it. |
| `behaviors.py`, `_ranges.py`, `_scrolling.py` | Reusable interaction and value mechanics. |
| `editing.py`, `editors.py` | Draft content, optional popup presentation, and concrete value editors. |
| `fields.py`, `dates.py` | Composed search fields and calendar content with date-picker presentation. |
| `_controls/indicators.py`, `navigation.py` | Interval selection, circular progress, segmented choices, breadcrumbs, and pagination. |
| `cards.py`, `feedback.py` | Exclusive choice cards, expandable content, semantic status badges, actionable alerts, and bounded meters. |
| `widgets.py`, `selection.py` | Stable public import facades, not places to add implementation bodies. |

Moved classes retain their public imports and `__module__` names, preserving
reflection, diagnostics, and subclass annotation resolution. `pysual.__init__`
keeps explicit exports for static typing. The built-in catalog is installed
once after those modules load; implementation files do not register themselves.

## Keep catalog discovery separate from application member names

Registering every new control as `Container.some_control()` would reserve
another name on every App and Container. New built-in catalog entries default
to registration without a factory alias. Existing aliases are explicit
compatibility choices in `_catalog.py`.

```python
from pysual import Container, Control, prop, register_control

class StatusBadge(Control):
    text: str = prop(default="Ready")

register_control(StatusBadge, factory=False)
panel = Container()
panel.status = panel.create(StatusBadge, text="Connected")
# Direct construction also works: StatusBadge(parent=panel, text="Connected").
```

`create` retains the concrete constructor's arguments and return type, runs the
normal attachment lifecycle, and disposes a newly constructed control if
attachment fails. It works without registration. Registration adds discovery:
`registered_controls()` includes all entries; `factories_only=True` selects
installed named aliases for tools such as the factory signature generator.

The public `register_control` default still installs an alias for compatibility.
Use `factory=False` for new controls. Catalog-only names can match ordinary
Container member names because they do not install Python attributes. A name
cannot refer to two registered types. Re-registering the same type/name updates
its authoring/factory policy; removing an alias does not remove catalog metadata.

## Compose behavior and appearance independently

`PressBehavior` recognizes pointer/key gestures and updates pressed state.
Controls retain their semantic actions, events, enabled checks, and painting.
`Button` composes it; Dropdown, ColorPicker, and NumericInput use its pointer
path with their own keyboard commands. Numeric input supplies hit parts so
pressing minus and releasing over plus cannot increment the value.

`style_fallbacks` lets a control reuse a themed appearance without inheriting
the corresponding visual class:

```python
class ActionTile(Control):
    style_fallbacks = ("Button",)
    # Compose PressBehavior and implement the tile's own schema/action/painting.
```

Fallbacks enter the base-to-derived style cascade before the declaring class's
own selector. Concrete rules still override their defaults. `style_excludes`
filters selectors from the cascade; CheckBox and Hyperlink use it to exclude
Button's surface styling. Subclasses inherit that policy or explicitly replace
it. Existing inheritance remains compatible because it also carries public
properties and theme contracts.

Stateful components belong to one instance, created in `_initialize`, used from
UI-owner hooks, and reset during destruction. Validation components must not
retain the owner: atomic updates validate a shallow copy with candidate values.
The immutable internal `NumericRange` receives these values explicitly, so
Slider, NumericInput, and ProgressBar can share rules without stale live-state
references. Keep `prop` declarations on controls; components do not inject
fields or create another lifecycle system.

## Reuse editor content inline or in a popup

`EditorContent` is an ordinary Container. It builds child controls and supplies
`read`, `is_untouched`, `equivalent`, and `error_message` hooks. `submit()` emits
a submission request that either an inline host or `EditorPopup` can handle.

```python
from pysual import ColorEditor, EditorPopup

inline = ColorEditor("#7289FA", parent=panel)
# Or give fresh content to a popup for any control with a compatible property:
# EditorPopup(owner, ColorEditor(owner.value)).show(owner)
```

The popup owns Apply/Enter acceptance, error presentation, user-origin property
assignment, and dismissal. The owner's schema still validates the final value.
NumericEditor and ColorEditor use the same shell; it contains no concrete-type
switch. Numeric drafts preserve untouched precision and external updates, while
color drafts keep synchronous text/channel coordination. These are content
policies, not popup lifecycle exceptions. A calendar or path editor can supply
different content without modifying the shell.

## Applying these boundaries in practice

These are architectural mappings, not commitments to every proposed feature.

| Ideas | Reuse and composition |
| --- | --- |
| Icon/split buttons, choice cards, ratings | Press gestures, hit parts, explicit style fallbacks; own action or selection semantics. |
| Date/time/color/icon pickers and inline edit | Draft content plus an inline host or anchored EditorPopup; owner validation at commit. |
| Search, autocomplete, token fields, command palettes | Text editing, collection navigation, and popup content; distinct query/selection models. |
| Lists, grids, trees, reorderable collections | Family-specific data models over shared scrolling, visible-item geometry, and input mechanics. |
| Range sliders, meters, gauges, progress | Shared numeric rules plus family-specific interaction and presentation. |
| Toolbars, inspectors, filters, wizards, cards | Containers composing controls, named handlers, and explicit semantic state. |
| Charts, legends, axes, brush/annotation tools | A chart family with data/scale/viewport parts; composition for surrounding controls. |
| Query/API/pipeline/schema editors | Composed tools over domain models, not new special cases in core input or host backends. |

[`examples/composite_control.py`](../examples/composite_control.py) demonstrates
two independent range fields assembled from a label, slider, and numeric input.
Small child adapters validate against the parent before committing an edit and
synchronize the parent and sibling immediately, preserving the incoming origin.
Public `changed` events notify application handlers after that coordination;
they do not synchronize the component's own values.
The test suite also mounts the same editor content inline and in a popup and
supplies a third editor implementation without changing the popup shell.

### Implemented catalog proofs

Thirteen ideas are implemented in the library and exercised together in
[`examples/catalog_controls.py`](../examples/catalog_controls.py):

| Control | Boundary exercised |
| --- | --- |
| `Rating` | A direct Control subclass composes PressBehavior with per-star hit parts, hover preview, keyboard selection, and its own portable painting. |
| `SearchField` | A Container owns a TextBox, search icon, and clear Button, exposing one text value and semantic change/submission events. Child edits synchronize before the next input is handled. |
| `DatePicker` and `CalendarEditor` | A picker supplies independent calendar draft content to the existing EditorPopup. The same content mounts inline; bounds validation remains on the picker at commit. |
| `RangeSlider` | An atomic interval uses NumericRange and PressBehavior, with independently selectable thumbs and one tuple change event. |
| `CircularProgress` | A determinate ring shares ProgressBar styles and numeric bounds, using bounded portable geometry. |
| `SegmentedControl` | A small choice set uses joined surfaces, per-part press matching, and keyboard selection. |
| `Breadcrumb` | An application-owned path emits ancestor navigation requests without mutating the path or invoking a filesystem. |
| `Pagination` | A bounded window of page buttons navigates arbitrarily large page counts without allocating one control per page. |
| `ChoiceCard` | Sibling groups reuse press gestures for exclusive selection, with descriptive text, icons, and keyboard navigation. |
| `Disclosure` | An owned header and content container preserve child state while hiding collapsed content from painting, picking, and keyboard navigation. |
| `Badge` | A noninteractive semantic status caption supports a presence dot and theme-specific tone parts. |
| `AlertBanner` | A persistent message owns matched action/dismiss hit regions and keyboard commands without changing the event router. |
| `Meter` | A measured level shares numeric bounds, optional ordered thresholds, and bounded segmented or continuous drawing. |

All 43 built-in types are discoverable, with the same 30 historical factory
aliases. The thirteen new controls are registered without Container aliases. Existing application
members named `rating`, `search_field`, or `date_picker` remain available.
They work with direct constructors, `Container.create`, and (for controls whose
inputs are schema properties) blueprints. No backend or input-router branches
were needed for these families.

The initial scope is whole-star ratings, plain-text search, one Gregorian
calendar date represented by a canonical ISO string, one ordered numeric interval,
determinate progress, text segments, path labels, and one-based pages. Search
results, breadcrumb destinations, filtering and page data belong to the
application; date ranges, locale formatting, and suggestion providers remain
separate requirements.

## Adding a family or control

1. Decide whether the feature is a leaf control, reusable content, a behavior,
   or a composite. Start from `Control`/`Container`, or an existing class whose
   complete public contract fits. Keep domain services outside the control.
2. Put implementation in a focused family. Reuse existing parts; extract new
   parts when actual implementations share a responsibility. Selection models,
   text adornments, and chart scales can evolve within their families as needed.
3. Keep validation in the schema/update path, input in the ordinary hooks, and
   drawing portable through `Painter`. Do not add a runtime branch per control.
4. Export the public type and add its metadata to `_catalog.py` with the default
   `legacy_factory=False`. The inspiration JSON is not an import manifest.
5. Run `python tools/generate_factory_types.py` if alias schemas changed. Imports
   and signatures derive from registered schemas rather than another type list.
6. Test the feature's semantics, composition isolation, atomic validation,
   lifetime, style fallback, public imports/typing, and relevant host behavior.
   New families should not need to modify an existing family's implementation.

See [the public composition example](api.md#composing-control-behavior) for the
press component's complete hooks.
