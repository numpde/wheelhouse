"""Present every diagnostic together when a source convention fails."""


def assert_no_findings(findings):
    if findings:
        raise AssertionError('\n'.join(findings))
