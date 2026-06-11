from automator import verify
from automator.model import StoryTask
from automator.policy import Policy, VerifyPolicy
from conftest import git, spec_path, write_spec, write_sprint


def make_task(paths, story_key="1-1-a"):
    task = StoryTask(story_key=story_key, epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def dev_result(sp):
    return {"workflow": "quick-dev", "spec_file": str(sp)}


def test_verify_dev_happy(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_missing_spec_file_claim(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, {})
    assert not out.ok and out.retryable and "missing spec_file" in out.reason


def test_verify_dev_spec_does_not_exist(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(project.project / "ghost.md"))
    assert not out.ok and "does not exist" in out.reason


def test_verify_dev_wrong_status(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "draft", task.baseline_commit)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "expected 'in-review'" in out.reason


def test_verify_dev_lying_baseline(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "deadbeef" * 5)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_no_changes(project):
    # Spec claims NO_VCS baseline (skips the mismatch check); everything is
    # committed, so there are no changes since the orchestrator's baseline.
    write_sprint(project, {"1-1-a": "review"})
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "artifacts")
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason


def test_verify_dev_sprint_not_synced(project):
    write_sprint(project, {"1-1-a": "in-progress"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "sprint-status" in out.reason


def test_verify_review_happy_and_commands(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    ok_policy = Policy(verify=VerifyPolicy(commands=("true",)))
    assert verify.verify_review(task, project, ok_policy).ok

    fail_policy = Policy(verify=VerifyPolicy(commands=("true", "false")))
    out = verify.verify_review(task, project, fail_policy)
    assert not out.ok and "verify command failed" in out.reason


def test_verify_review_spec_not_done(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_review_sprint_not_done(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "sprint-status" in out.reason


def test_reset_hard_keeps_automator_dir(project):
    baseline = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("dirty\n")
    (project.project / "junk.txt").write_text("untracked\n")
    keep = project.project / ".automator" / "runs" / "r1"
    keep.mkdir(parents=True)
    (keep / "state.json").write_text("{}")

    verify.reset_hard(project.project, baseline)
    assert (project.project / "src.txt").read_text() == "original\n"
    assert not (project.project / "junk.txt").exists()
    assert (keep / "state.json").exists()


def test_commit_story(project):
    task = make_task(project)
    (project.project / "src.txt").write_text("done work\n")
    sha = verify.commit_story(project.project, task)
    assert sha != task.baseline_commit
    assert verify.worktree_clean(project.project)


def test_read_frontmatter_tolerates_garbage(project):
    p = project.project / "x.md"
    p.write_text("no frontmatter here")
    assert verify.read_frontmatter(p) == {}
    p.write_text("---\n: : :\nbroken yaml [\n---\nbody")
    assert verify.read_frontmatter(p) == {}
