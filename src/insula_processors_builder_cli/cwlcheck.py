"""Structural checks for an Insula Application Package (.cwl).

Mirrors the rules the platform enforces server-side (poieo `CwlValidator`,
`CwlToPlatformServiceMapper`, `PlatformServiceValidator`). The platform rejects a
bad CWL with a BODILESS HTTP 400 - the reason is only in its server logs - so the
user's single chance of learning WHY is here, on their own machine, before the
deploy POST and ideally before a full pipeline run.

Every rule below is transcribed from that server-side code, not invented. Keep the
comments naming the source so a drift is traceable. Rules that depend on platform
state (a duplicate service name, an unknown user mount) are NOT checked here: they
cannot be known locally.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import yaml

# PlatformServiceValidator.MAX_CWL_SERVICE_DESCRIPTION_LENGTH: the Workflow `doc`
# becomes PlatformService.description, which is capped.
MAX_DESCRIPTION_LENGTH = 255

# CwlValidator.SUPPORTED_COMMAND_LINE_TOOL_REQUIREMENTS.
SUPPORTED_REQUIREMENTS = (
    "DockerRequirement",
    "ResourceRequirement",
    "NetworkAccess",
    "EnvVarRequirement",
    "InitialWorkDirRequirement",
)

# CwlValidator.SUPPORTED_OUTPUT_TYPES.
SUPPORTED_OUTPUT_TYPES = ("File", "Directory")

# The CWL type names CwlToPlatformServiceMapper.mapCwlType understands. CWL type
# names are case-sensitive: `String` is not `string`, and the platform maps an
# unknown name to a parameter with no data type instead of failing loudly.
CWL_TYPES = (
    "null", "boolean", "int", "long", "float", "double", "string", "File", "Directory",
)

# CwlValidator.SUPPORTED_SCATTER_METHODS.
SUPPORTED_SCATTER_METHODS = ("dotproduct",)

IMAGE_TOKEN = "__IMAGE__"


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader that also rejects duplicate mapping keys.

    PyYAML keeps the last one silently; the platform's snakeyaml raises
    DuplicateKeyException, which CwlService turns into a 400. Match the platform.

    It subclasses SafeLoader and overrides only the mapping constructor, so the
    `yaml.load` call below builds plain dicts/lists and never constructs arbitrary
    Python objects. Do not rebase this on Loader or UnsafeLoader.
    """


def _construct_mapping(loader: yaml.Loader, node: yaml.Node, deep: bool = False) -> Dict[str, Any]:
    mapping: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key '{key}' at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def check_cwl(text: str, *, expect_image_token: bool) -> List[str]:
    """Return a list of problems with the CWL. Empty list means it passed.

    `expect_image_token` is True for an author's pre-build .cwl (the `__IMAGE__`
    token MUST still be there; the pipeline injects the published image into it)
    and False for a pipeline-finalized CWL (the token MUST be gone).
    """
    problems: List[str] = []
    document = _parse(text, problems)
    if document is None:
        return problems

    pair = _find_workflow_and_tool(document, problems)
    _check_image_token(text, expect_image_token, problems)
    if pair is None:
        return problems
    workflow, tool = pair

    _check_description(workflow, problems)
    _check_requirements(tool, expect_image_token, problems)
    step = _check_step(workflow, tool, problems)
    _check_inputs(workflow, tool, step, problems)
    _check_outputs(workflow, tool, step, problems)
    _check_scatter(workflow, step, problems)
    return problems


def _parse(text: str, problems: List[str]) -> Optional[Dict[str, Any]]:
    try:
        document = yaml.load(text, Loader=_NoDuplicateKeyLoader)
    except yaml.YAMLError as exc:
        problems.append(f"not valid YAML: {exc}")
        return None
    if not isinstance(document, dict):
        problems.append("the CWL must be a YAML mapping at the top level")
        return None
    return document


def _find_workflow_and_tool(
    document: Dict[str, Any], problems: List[str]
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """CwlValidator.validate: exactly one Workflow and one CommandLineTool."""
    graph = document.get("$graph")
    if not isinstance(graph, list):
        problems.append("the CWL must declare a '$graph' list holding the Workflow and the CommandLineTool")
        return None
    workflows = [e for e in graph if isinstance(e, dict) and e.get("class") == "Workflow"]
    tools = [e for e in graph if isinstance(e, dict) and e.get("class") == "CommandLineTool"]
    if len(workflows) != 1:
        problems.append(f"exactly one 'class: Workflow' is required (found {len(workflows)})")
    if len(tools) != 1:
        problems.append(f"exactly one 'class: CommandLineTool' is required (found {len(tools)})")
    if len(workflows) != 1 or len(tools) != 1:
        return None
    if not _identifier(workflows[0].get("id")):
        problems.append("the Workflow needs an 'id' (it becomes the process id on the platform)")
    if not _identifier(tools[0].get("id")):
        problems.append("the CommandLineTool needs an 'id' (the Workflow step's 'run' points at it)")
    return workflows[0], tools[0]


def _check_image_token(text: str, expect_image_token: bool, problems: List[str]) -> None:
    count = text.count(IMAGE_TOKEN)
    if expect_image_token and count != 1:
        problems.append(
            f"exactly one {IMAGE_TOKEN} token is required (found {count}); the pipeline "
            "injects the published image there"
        )
    if not expect_image_token and count:
        problems.append(f"the {IMAGE_TOKEN} token is still present (image was not injected)")


def _check_description(workflow: Dict[str, Any], problems: List[str]) -> None:
    """PlatformServiceValidator.validateCwlDescriptionLength, and the mapper's
    `(String) workflow.getDoc()` cast, which a list-valued doc would break."""
    doc = workflow.get("doc")
    if doc is None:
        return
    if not isinstance(doc, str):
        problems.append("the Workflow 'doc' must be a single string (it becomes the process description)")
        return
    if len(doc) > MAX_DESCRIPTION_LENGTH:
        problems.append(
            f"the Workflow 'doc' is {len(doc)} characters; the platform caps the process "
            f"description at {MAX_DESCRIPTION_LENGTH} and rejects the deploy above it"
        )


def _check_requirements(tool: Dict[str, Any], expect_image_token: bool, problems: List[str]) -> None:
    """CwlValidator.validateCommandLineToolRequirements."""
    requirements = tool.get("requirements")
    if not isinstance(requirements, dict):
        problems.append("the CommandLineTool needs a 'requirements' mapping with a DockerRequirement")
        return
    for name in requirements:
        if name not in SUPPORTED_REQUIREMENTS:
            problems.append(
                f"unsupported CommandLineTool requirement '{name}'; the platform accepts only "
                + ", ".join(SUPPORTED_REQUIREMENTS)
            )
    _check_initial_work_dir(requirements.get("InitialWorkDirRequirement"), problems)
    docker = requirements.get("DockerRequirement")
    if not isinstance(docker, dict):
        problems.append("a DockerRequirement is required in the CommandLineTool")
        return
    docker_pull = docker.get("dockerPull")
    if not docker_pull or not isinstance(docker_pull, str):
        problems.append("DockerRequirement.dockerPull is required")
    elif expect_image_token and docker_pull.strip() != IMAGE_TOKEN:
        problems.append(
            f"DockerRequirement.dockerPull must be exactly {IMAGE_TOKEN} before the build "
            f"(found '{docker_pull}'); the pipeline replaces it with the published image"
        )


def _check_initial_work_dir(requirement: Any, problems: List[str]) -> None:
    """CwlValidator.validateInitialWorkDirRequirementDirectories: every listed
    Directory needs a location and a basename, and the basename is a relative path.

    Whether the mount named by `location` actually exists on the platform cannot be
    checked here; an unknown one is rejected server-side with the same empty 400.
    """
    if requirement is None:
        return
    if not isinstance(requirement, dict):
        problems.append("InitialWorkDirRequirement must be a mapping with a 'listing'")
        return
    for entry in requirement.get("listing") or []:
        if not isinstance(entry, dict) or entry.get("class") != "Directory":
            continue
        location = entry.get("location")
        basename = entry.get("basename")
        if not location or not isinstance(location, str):
            problems.append("an InitialWorkDirRequirement Directory has no 'location'")
        if not basename or not isinstance(basename, str):
            problems.append("an InitialWorkDirRequirement Directory has no 'basename'")
        elif basename.startswith("/"):
            problems.append(
                f"the InitialWorkDirRequirement Directory basename '{basename}' must be a "
                "relative path (it cannot start with '/')"
            )


def _check_step(
    workflow: Dict[str, Any], tool: Dict[str, Any], problems: List[str]
) -> Optional[Dict[str, Any]]:
    """CwlValidator.validateWorkflowStep: one step, referencing the tool."""
    steps = _as_map(workflow.get("steps"))
    if len(steps) != 1:
        problems.append(f"the Workflow must contain exactly one step (found {len(steps)})")
        return None
    step = next(iter(steps.values()))
    if not isinstance(step, dict):
        problems.append("the Workflow step must be a mapping")
        return None
    run = step.get("run")
    if not isinstance(run, str) or _identifier(run) != _identifier(tool.get("id")):
        problems.append(
            f"the Workflow step's 'run' must reference the CommandLineTool id "
            f"('#{_identifier(tool.get('id'))}'), found '{run}'"
        )
    return step


def _check_inputs(
    workflow: Dict[str, Any],
    tool: Dict[str, Any],
    step: Optional[Dict[str, Any]],
    problems: List[str],
) -> None:
    """CwlValidator.inputsMatch (ids), plus a type-agreement check the platform does
    NOT make: a Workflow/CommandLineTool type mismatch is accepted server-side and
    then mis-mapped, so it has to be caught here."""
    workflow_inputs = _as_map(workflow.get("inputs"))
    tool_inputs = _as_map(tool.get("inputs"))
    step_inputs = _as_map(step.get("in")) if step else {}
    # The scattered input is the one legitimate mismatch: the Workflow takes the
    # array, the CommandLineTool takes one element of it per fan-out task.
    scattered = _identifier(step.get("scatter")) if step else ""

    for name, parameter in tool_inputs.items():
        tool_type = _describe_type(parameter, f"CommandLineTool input '{name}'", problems)
        if step is not None and name not in step_inputs:
            problems.append(f"CommandLineTool input '{name}' is missing from the Workflow step's 'in'")
            continue
        source = _identifier(_source_of(step_inputs.get(name))) if step else None
        if step is None:
            continue
        if source not in workflow_inputs:
            problems.append(
                f"the Workflow step maps input '{name}' from '{source}', which is not a Workflow input"
            )
            continue
        workflow_type = _describe_type(
            workflow_inputs[source], f"Workflow input '{source}'", problems
        )
        if not tool_type or not workflow_type:
            continue
        if name == scattered:
            if workflow_type != f"{tool_type}[]":
                problems.append(
                    f"scattered input '{name}' is '{workflow_type}' on the Workflow; with scatter "
                    f"it must be the array of the CommandLineTool type ('{tool_type}[]')"
                )
        elif tool_type != workflow_type:
            problems.append(
                f"input '{name}' is '{workflow_type}' on the Workflow but '{tool_type}' on the "
                "CommandLineTool; both sides must declare the same type"
            )


def _check_outputs(
    workflow: Dict[str, Any],
    tool: Dict[str, Any],
    step: Optional[Dict[str, Any]],
    problems: List[str],
) -> None:
    """CwlValidator.outputsMatch: every tool output reaches a Workflow output, and
    only File/Directory outputs are supported."""
    workflow_outputs = _as_map(workflow.get("outputs"))
    tool_outputs = _as_map(tool.get("outputs"))
    step_outputs = [_identifier(o) for o in (step.get("out") or []) if step] if step else []
    exported = {
        _identifier(src).split("/")[-1]
        for parameter in workflow_outputs.values()
        for src in _output_sources(parameter)
    }

    for name, parameter in tool_outputs.items():
        output_type = _describe_type(parameter, f"CommandLineTool output '{name}'", problems)
        if output_type and output_type.rstrip("[]") not in SUPPORTED_OUTPUT_TYPES:
            problems.append(
                f"CommandLineTool output '{name}' is '{output_type}'; outputs must be "
                + " or ".join(SUPPORTED_OUTPUT_TYPES)
            )
        if step is not None and name not in step_outputs:
            problems.append(f"CommandLineTool output '{name}' is missing from the Workflow step's 'out'")
        elif name not in exported:
            problems.append(
                f"CommandLineTool output '{name}' is not exported by any Workflow output "
                "('outputSource: <step>/" + name + "')"
            )


def _check_scatter(
    workflow: Dict[str, Any], step: Optional[Dict[str, Any]], problems: List[str]
) -> None:
    """CwlValidator.validateScatterStep: dotproduct only, array scatter input, all
    Workflow outputs arrays."""
    if step is None or not step.get("scatter"):
        return
    method = step.get("scatterMethod")
    if method not in SUPPORTED_SCATTER_METHODS:
        problems.append(
            f"scatterMethod must be one of {', '.join(SUPPORTED_SCATTER_METHODS)} (found '{method}')"
        )
    scattered = _identifier(step.get("scatter"))
    source = _identifier(_source_of(_as_map(step.get("in")).get(scattered)))
    workflow_inputs = _as_map(workflow.get("inputs"))
    if source not in workflow_inputs:
        problems.append(f"the scatter input '{scattered}' is not mapped from a Workflow input")
    else:
        scatter_type = _describe_type(workflow_inputs[source], f"Workflow input '{source}'", problems)
        if scatter_type and not scatter_type.endswith("[]"):
            problems.append(f"the scatter input '{source}' must be an array type (found '{scatter_type}')")
    for name, parameter in _as_map(workflow.get("outputs")).items():
        output_type = _describe_type(parameter, f"Workflow output '{name}'", problems)
        if output_type and not output_type.endswith("[]"):
            problems.append(
                f"with scatter, every Workflow output must be an array; '{name}' is '{output_type}'"
            )


def _describe_type(parameter: Any, where: str, problems: List[str]) -> Optional[str]:
    """Canonical type string ('Directory', 'string[]', 'enum'), or None if unknown.

    Reports unknown names, which is how a capitalised `String` is caught: the
    platform accepts it as an opaque schema reference and maps it to nothing.
    """
    if not isinstance(parameter, dict) or "type" not in parameter:
        problems.append(f"{where} has no 'type'")
        return None
    return _canonical_type(parameter["type"], where, problems)


def _canonical_type(node: Any, where: str, problems: List[str]) -> Optional[str]:
    if isinstance(node, list):
        members = [m for m in node if m != "null"]
        if len(members) != 1:
            problems.append(f"{where} has an unsupported union type")
            return None
        return _canonical_type(members[0], where, problems)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "array":
            item = _canonical_type(node.get("items"), where, problems)
            return f"{item}[]" if item else None
        if kind == "enum":
            return "enum"
        problems.append(f"{where} has an unsupported type '{kind}'")
        return None
    if not isinstance(node, str):
        problems.append(f"{where} has an unreadable type")
        return None

    base = node[:-1] if node.endswith("?") else node
    is_array = base.endswith("[]")
    base = base[:-2] if is_array else base
    if base not in CWL_TYPES:
        hint = next((t for t in CWL_TYPES if t.lower() == base.lower()), None)
        problems.append(
            f"{where} has type '{base}', which is not a CWL type"
            + (f" (CWL type names are case-sensitive: use '{hint}')" if hint else "")
        )
        return None
    return f"{base}[]" if is_array else base


def _as_map(node: Any) -> Dict[str, Any]:
    """CWL allows the mapping form ({id: {...}}) or the list form ([{id: ...}]).

    Normalise both to a mapping keyed by the unqualified id. A step's `in` also
    allows the shorthand {name: source}, which stays as a plain string value.
    """
    if isinstance(node, dict):
        return {_identifier(k): v for k, v in node.items()}
    if isinstance(node, list):
        entries = {}
        for item in node:
            if isinstance(item, dict) and "id" in item:
                entries[_identifier(item["id"])] = item
        return entries
    return {}


def _source_of(step_input: Any) -> Any:
    """A step input is either `name: source` or `name: {source: ...}`."""
    if isinstance(step_input, dict):
        return step_input.get("source")
    return step_input


def _output_sources(parameter: Any) -> List[Any]:
    if not isinstance(parameter, dict):
        return []
    source = parameter.get("outputSource")
    if isinstance(source, list):
        return source
    return [source] if source else []


def _identifier(value: Any) -> str:
    """Strip the '#fragment' prefix CWL ids carry, keeping the bare name."""
    if not isinstance(value, str):
        return ""
    return value.split("#")[-1]
