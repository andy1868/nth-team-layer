"""template_demo.py — end-to-end walk-through of MissionTemplate + Review.

Run from the repo root:

    python examples/template_demo.py

It uses a temporary directory so it leaves no artifacts behind.
"""

import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# Make team_layer / nth_dao importable when run directly
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import nth_dao as nth
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.orchestration import (
    IOField,
    MissionStore,
    StepSkeleton,
    TemplateType,
    mint_template,
)


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    if not crypto_available():
        print("ERROR: PyNaCl is required for this demo.")
        print("Install with: pip install nth-dao[crypto]")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        store = MissionStore(str(td / "missions"))
        alice = AgentIdentity.generate(label="Alice (publisher)")
        bob = AgentIdentity.generate(label="Bob (claimant)")
        carol = AgentIdentity.generate(label="Carol (reviewer)")
        dave = AgentIdentity.generate(label="Dave (reviewer)")

        # ─── 1. Alice publishes two templates ───
        section("1. Alice publishes 2 templates (signed)")
        code_review = mint_template(
            alice,
            template_id="code-review",
            version="1.0.0",
            name="Code Review",
            description="Read a diff and flag issues by severity.",
            template_type=TemplateType.AGENT_TASK,
            category="code_review",
            tags=["python", "security"],
            required_capabilities=["code_review"],
            inputs={
                "diff_url": IOField(
                    description="URL of the diff or PR to review",
                    type="string", required=True,
                ),
            },
            outputs={
                "severity": IOField(
                    description="Worst severity found",
                    type="enum", values=["low", "med", "high"],
                ),
                "review_notes": IOField(
                    description="Free-form review notes",
                    type="string",
                ),
            },
            steps=[
                StepSkeleton(
                    id="review",
                    description="Read the diff, write review notes, set severity",
                    required_capabilities=["code_review"],
                    inputs_from={"diff_url": "input:diff_url"},
                ),
            ],
            suggested_reward=5.0,
            suggested_deadline_hours=24,
        )
        store.publish_template(code_review)
        print(f"  published code-review@1.0.0  "
              f"verify={code_review.verify_signature()}")

        data_cleanup = mint_template(
            alice,
            template_id="data-cleanup",
            version="0.2.0",
            name="Data Cleanup",
            description="Clean a CSV: trim whitespace, dedupe, normalize.",
            template_type=TemplateType.AGENT_TASK,
            category="data_cleanup",
            tags=["pandas"],
            required_capabilities=["python", "pandas"],
            inputs={
                "csv_path": IOField(
                    description="Path to input CSV", type="string", required=True,
                ),
            },
            suggested_reward=3.0,
        )
        store.publish_template(data_cleanup)
        print(f"  published data-cleanup@0.2.0  "
              f"verify={data_cleanup.verify_signature()}")

        # ─── 2. Browse the store (empty stats so far) ───
        section("2. Bob browses the store")
        for entry in store.browse_templates():
            t = entry["template"]
            s = entry["stats"]
            print(f"  [{t.category:13s}] {t.template_id}@{t.version}  "
                  f"reward={t.suggested_reward}  rating={s.weighted_average:.1f}({s.review_count})")

        # ─── 3. Bob instantiates code-review ───
        section("3. Bob instantiates code-review (template_lock snapshot)")
        m = store.instantiate(
            "code-review",
            owner=str(bob.agent_id),
            inputs={"diff_url": "https://github.com/foo/bar/pull/42"},
        )
        print(f"  mission_id={m.id[:12]}")
        print(f"  template_id={m.template_id}@{m.template_version}")
        print(f"  template_lock.publisher_sig={m.template_lock['publisher_sig'][:24]}...")
        print(f"  steps materialised from skeleton: {[s.id for s in m.steps]}")

        # ─── 4. Two independent reviewers ───
        section("4. Carol and Dave review Bob's mission")
        r1 = store.review_mission(m.id, reviewer=carol, score=4.5,
                                  feedback="caught 3 edge cases")
        r2 = store.review_mission(m.id, reviewer=dave, score=4.0,
                                  feedback="missed one race condition")
        print(f"  carol score=4.5  verify={r1.verify_signature()}")
        print(f"  dave  score=4.0  verify={r2.verify_signature()}")

        # ─── 5. Browse again — stats updated ───
        section("5. Browse store again (stats reflect reviews)")
        for entry in store.browse_templates(sort_by="rating"):
            t = entry["template"]
            s = entry["stats"]
            print(f"  [{t.category:13s}] {t.template_id}@{t.version}  "
                  f"rating={s.weighted_average:.2f}  reviews={s.review_count}  "
                  f"unique_reviewers={s.unique_reviewers}")

        # ─── 6. Alice deprecates v1.0.0 and publishes v2.0.0 ───
        section("6. Alice deprecates v1.0.0, publishes v2.0.0")
        store.templates.deprecate(
            alice, "code-review", "1.0.0",
            reason="use v2 which requires `security` capability",
        )
        code_review_v2 = mint_template(
            alice,
            template_id="code-review",
            version="2.0.0",
            name="Code Review v2",
            description="Security-aware code review.",
            category="code_review",
            tags=["python", "security"],
            required_capabilities=["code_review", "security"],
            inputs={
                "diff_url": IOField(type="string", required=True,
                                    description="diff URL"),
            },
            suggested_reward=7.0,
            supersedes=["code-review-v1.0.0"],
        )
        store.publish_template(code_review_v2)

        print("  default browse (deprecated hidden):")
        for entry in store.browse_templates():
            t = entry["template"]
            print(f"    {t.template_id}@{t.version}  deprecated={t.deprecated}")

        print("  with include_deprecated=True:")
        for entry in store.browse_templates(include_deprecated=True):
            t = entry["template"]
            print(f"    {t.template_id}@{t.version}  deprecated={t.deprecated}")

        # ─── 7. Browse by category + by tag ───
        section("7. Filtered browse")
        only_cr = store.browse_templates(category="code_review")
        print(f"  category=code_review → {len(only_cr)} template(s)")
        only_security = store.browse_templates(tags=["security"])
        print(f"  tags=[security]      → {len(only_security)} template(s)")

        # ─── 8. Latest-version auto-selection ───
        section("8. Bob instantiates latest version implicitly")
        m2 = store.instantiate(
            "code-review",   # version omitted → uses latest = 2.0.0
            owner=str(bob.agent_id),
            inputs={"diff_url": "https://example.com/pr/99"},
        )
        print(f"  bob instantiated code-review @ {m2.template_version}")

        # ─── 9. Per-template stats ───
        section("9. Stats per template version")
        stats_v1 = store.template_stats("code-review", "1.0.0")
        stats_v2 = store.template_stats("code-review", "2.0.0")
        print(f"  v1.0.0  installs={stats_v1.install_count}  "
              f"reviews={stats_v1.review_count}  avg={stats_v1.average_rating:.2f}")
        print(f"  v2.0.0  installs={stats_v2.install_count}  "
              f"reviews={stats_v2.review_count}  avg={stats_v2.average_rating:.2f}")

        # ─── 10. Personal history ───
        section("10. Bob's mission history")
        for m in store.my_history(str(bob.agent_id)):
            print(f"  {m.id[:8]}  template={m.template_id}@{m.template_version}  "
                  f"status={m.status}")

        # ─── 11. Index files on disk ───
        section("11. On-disk index files (TUF / F-Droid style)")
        idx_t = store.templates.load_index()
        idx_r = store.reviews.load_index()
        print(f"  _template_index.json  version={idx_t.get('version')}  "
              f"meta_count={len(idx_t.get('meta', {}))}")
        print(f"  _review_index.json    entries={len(idx_r)}")

    print()
    print("=" * 70)
    print("  v0.9.3 MissionTemplate end-to-end OK")
    print("=" * 70)


if __name__ == "__main__":
    main()
