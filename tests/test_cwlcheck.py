"""Rule-level tests for the CWL structural checks.

Each rule mirrors something the platform enforces server-side and rejects with a
BODILESS 400, so a regression here is invisible to users until a deploy fails with
no reason attached. Keep one test per rule.
"""

from __future__ import annotations

import pytest

from insula_processors_builder_cli.cwlcheck import MAX_DESCRIPTION_LENGTH, check_cwl

_VALID = """cwlVersion: v1.2
$graph:
- class: Workflow
  id: tiny
  label: Tiny
  doc: A tiny processor.
  inputs:
    catalogue:
      type: Directory
    threshold:
      type: float
  outputs:
    result:
      type: Directory
      outputSource: process/result
  steps:
    process:
      run: '#main'
      in:
        catalogue: catalogue
        threshold: threshold
      out:
        - result
- class: CommandLineTool
  id: main
  requirements:
    DockerRequirement:
      dockerPull: reg/eopaas/eopaas/tiny:abc12345
    NetworkAccess:
      networkAccess: false
  baseCommand: run.sh
  inputs:
    catalogue:
      type: Directory
      inputBinding:
        position: 1
    threshold:
      type: float
      inputBinding:
        position: 2
  outputs:
    result:
      type: Directory
      outputBinding:
        glob: ./outDir/result/
"""


def _check(text: str) -> list:
    return check_cwl(text, expect_image_token=False)


def test_valid_package_has_no_problems():
    assert _check(_VALID) == []


def test_description_at_the_cap_is_accepted():
    assert _check(_VALID.replace("A tiny processor.", "x" * MAX_DESCRIPTION_LENGTH)) == []


def test_description_over_the_cap_is_rejected():
    # The exact failure that made a real deploy return an unexplained 400.
    problems = _check(_VALID.replace("A tiny processor.", "x" * (MAX_DESCRIPTION_LENGTH + 1)))
    assert any("caps the process description" in p for p in problems)


def test_multi_line_doc_is_rejected():
    # The mapper casts the doc to a String; a list-valued doc breaks it server-side.
    problems = _check(_VALID.replace("doc: A tiny processor.", "doc:\n  - one\n  - two"))
    assert any("must be a single string" in p for p in problems)


def test_capitalised_type_is_rejected_with_a_hint():
    # CWL type names are case-sensitive; the platform silently maps `String` to
    # nothing instead of failing, so the parameter reaches Insula unusable.
    problems = _check(_VALID.replace("type: float", "type: Float"))
    assert any("case-sensitive" in p and "'float'" in p for p in problems)


def test_workflow_and_tool_type_mismatch_is_rejected():
    problems = _check(_VALID.replace("      type: Directory\n      inputBinding", "      type: Directory[]\n      inputBinding"))
    assert any("on the Workflow but" in p for p in problems)


def test_unsupported_output_type_is_rejected():
    problems = _check(
        _VALID.replace("    result:\n      type: Directory\n      outputBinding", "    result:\n      type: string\n      outputBinding")
    )
    assert any("outputs must be File or Directory" in p for p in problems)


def test_unsupported_requirement_is_rejected():
    problems = _check(_VALID.replace("    NetworkAccess:\n      networkAccess: false", "    InlineJavascriptRequirement: {}"))
    assert any("unsupported CommandLineTool requirement" in p for p in problems)


def test_step_not_referencing_the_tool_is_rejected():
    problems = _check(_VALID.replace("run: '#main'", "run: '#other'"))
    assert any("must reference the CommandLineTool id" in p for p in problems)


def test_tool_input_missing_from_the_step_is_rejected():
    problems = _check(_VALID.replace("        threshold: threshold\n", ""))
    assert any("missing from the Workflow step's 'in'" in p for p in problems)


def test_output_not_exported_by_the_workflow_is_rejected():
    problems = _check(_VALID.replace("      outputSource: process/result", "      outputSource: process/other"))
    assert any("not exported by any Workflow output" in p for p in problems)


def test_duplicate_key_is_rejected():
    # snakeyaml raises on the platform side; PyYAML would silently keep the last one.
    problems = _check(_VALID.replace("  id: main\n", "  id: main\n  id: main2\n"))
    assert any("duplicate key" in p for p in problems)


def test_absolute_initial_work_dir_basename_is_rejected():
    requirement = (
        "    InitialWorkDirRequirement:\n"
        "      listing:\n"
        "        - class: Directory\n"
        "          location: ./mount/userMount\n"
        "          basename: /absolute/path\n"
    )
    problems = _check(_VALID.replace("    NetworkAccess:\n      networkAccess: false\n", requirement))
    assert any("cannot start with '/'" in p for p in problems)


def test_image_token_expectations_are_direction_specific():
    author = _VALID.replace("reg/eopaas/eopaas/tiny:abc12345", "__IMAGE__")
    assert check_cwl(author, expect_image_token=True) == []
    assert any("still present" in p for p in check_cwl(author, expect_image_token=False))
    assert any("exactly one __IMAGE__" in p for p in check_cwl(_VALID, expect_image_token=True))


def test_a_comment_naming_the_token_is_not_counted():
    # The scaffolded header documents the token by name. Only keys and values are
    # counted, because only those receive the finalize step's global substitution.
    comment = "# dockerPull carries the __IMAGE__ token until the pipeline fills it in.\n"
    author = _VALID.replace("reg/eopaas/eopaas/tiny:abc12345", "__IMAGE__")
    assert check_cwl(comment + author, expect_image_token=True) == []
    assert check_cwl(comment + _VALID, expect_image_token=False) == []


def test_a_stray_token_in_a_value_is_counted():
    # A second token in a real value would have the published image reference
    # injected into it too, so it must still fail.
    author = _VALID.replace("reg/eopaas/eopaas/tiny:abc12345", "__IMAGE__")
    stray = author.replace("baseCommand: run.sh", "baseCommand: run.sh --tag __IMAGE__")
    problems = check_cwl(stray, expect_image_token=True)
    assert any("exactly one __IMAGE__" in p and "found 2" in p for p in problems)


def test_dockerpull_must_be_the_bare_token_before_the_build():
    # A hardcoded image would smuggle an unscanned reference past the pipeline,
    # which fails the run at finalize_cwl; catch it locally instead.
    author = _VALID.replace("dockerPull: reg/eopaas/eopaas/tiny:abc12345", "dockerPull: my/own:latest # __IMAGE__")
    problems = check_cwl(author, expect_image_token=True)
    assert any("must be exactly __IMAGE__" in p for p in problems)


@pytest.mark.parametrize(
    "text",
    [
        "cwlVersion: v1.2\n",  # no $graph
        "- class: Workflow\n",  # not a mapping
        "$graph: [\n",  # not YAML
    ],
)
def test_unusable_documents_are_reported(text):
    assert _check(text)


_SCATTER = _VALID.replace(
    "    catalogue:\n      type: Directory\n    threshold",
    "    catalogue:\n      type: Directory[]\n    threshold",
).replace(
    "      run: '#main'",
    "      run: '#main'\n      scatter: catalogue\n      scatterMethod: dotproduct",
).replace(
    "    result:\n      type: Directory\n      outputSource",
    "    result:\n      type: Directory[]\n      outputSource",
)


def test_scatter_package_is_accepted():
    # With scatter the Workflow takes the array and the tool takes one element:
    # the one legitimate type difference between the two sides.
    assert _check(_SCATTER) == []


def test_scatter_requires_an_array_workflow_input():
    problems = _check(_SCATTER.replace("type: Directory[]\n    threshold", "type: Directory\n    threshold"))
    assert any("must be the array of the CommandLineTool type" in p for p in problems)


def test_scatter_requires_array_workflow_outputs():
    problems = _check(_SCATTER.replace("    result:\n      type: Directory[]", "    result:\n      type: Directory"))
    assert any("every Workflow output must be an array" in p for p in problems)


def test_unsupported_scatter_method_is_rejected():
    problems = _check(_SCATTER.replace("scatterMethod: dotproduct", "scatterMethod: nested_crossproduct"))
    assert any("scatterMethod must be one of dotproduct" in p for p in problems)
