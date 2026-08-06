from dataclasses import dataclass


@dataclass
class Options:
    json: bool = False
    no_input: bool = False
    workspace: str = ""
    yes: bool = False


options = Options()


def configure(*, json_output: bool, no_input: bool, workspace: str, yes: bool):
    global options
    options = Options(json=json_output, no_input=no_input, workspace=workspace, yes=yes)
