"""insula-processor-cli: launch the cgi-italy processor pipeline and publish its CWL.

The api token used to authenticate the final CWL POST never leaves this machine:
it is read locally and sent straight to the publish endpoint. It is never passed
to GitHub Actions.
"""

__version__ = "0.1.0"
