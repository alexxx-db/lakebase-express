"""A routine that changes kind: dropping the stale one, and reporting it if it survives.

`CREATE OR REPLACE` replaces a routine of the same kind only. When a translation
reshapes a procedure into a function, re-applying it to a target that still holds the
procedure either fails (42P13, same signature) or leaves **both** (different
signature) — and then PostgreSQL picks between them by argument type, so a caller can
reach the stale one and get 42809 on every request.
"""
from backend.migration.executor import _item_sql
from backend.migration.models import ObjectKind, PlanItem
from backend.schema_migration.routine_sql import kind_change_preamble, with_kind_guard

FUNCTION_SQL = (
    'CREATE OR REPLACE FUNCTION "public"."usp_ItemReport"(p_category text)\n'
    "RETURNS TABLE(item_id int) LANGUAGE plpgsql AS $$ BEGIN RETURN QUERY SELECT 1; END $$;"
)
PROCEDURE_SQL = (
    "CREATE OR REPLACE PROCEDURE public.usp_set_status(p_id int) "
    "LANGUAGE plpgsql AS $$ BEGIN END $$;"
)


# --- The preamble ------------------------------------------------------------------


def test_a_procedure_reshaped_into_a_function_drops_the_stale_procedure():
    sql = kind_change_preamble("procedure", FUNCTION_SQL)
    assert "p.prokind = 'p'" in sql          # the kind being replaced
    assert "DROP PROCEDURE" in sql
    assert "p.proname = 'usp_ItemReport'" in sql   # quoted in the DDL, so case kept
    assert "n.nspname = 'public'" in sql
    assert "CASCADE" not in sql              # a dependency must fail loudly


def test_an_unquoted_name_is_matched_folded():
    sql = kind_change_preamble(
        "procedure", "CREATE OR REPLACE FUNCTION public.Usp_Mixed() RETURNS int AS $$ $$;"
    )
    assert "p.proname = 'usp_mixed'" in sql


def test_a_missing_schema_qualifier_falls_back_to_the_target_schema():
    sql = kind_change_preamble(
        "procedure", "CREATE OR REPLACE FUNCTION plain() RETURNS int AS $$ $$;",
        default_schema="app",
    )
    assert "n.nspname = 'app'" in sql


def test_no_preamble_when_the_kind_is_unchanged():
    assert kind_change_preamble("procedure", PROCEDURE_SQL) == ""
    assert kind_change_preamble(
        "function", "CREATE FUNCTION public.f() RETURNS int AS $$ $$;"
    ) == ""


def test_no_preamble_for_views_or_triggers():
    assert kind_change_preamble("view", "CREATE OR REPLACE VIEW public.v AS SELECT 1;") == ""
    assert kind_change_preamble(
        "trigger",
        "CREATE OR REPLACE FUNCTION public.trg_fn() RETURNS trigger AS $$ $$;\n"
        "CREATE OR REPLACE TRIGGER trg AFTER INSERT ON public.t "
        "FOR EACH ROW EXECUTE FUNCTION public.trg_fn();",
    ) == ""


def test_with_kind_guard_keeps_the_original_sql_intact():
    guarded = with_kind_guard("procedure", FUNCTION_SQL)
    assert FUNCTION_SQL in guarded
    assert guarded.index("DO $lbx_kind$") < guarded.index("CREATE OR REPLACE FUNCTION")


def test_with_kind_guard_is_a_no_op_without_a_kind_change():
    assert with_kind_guard("procedure", PROCEDURE_SQL) == PROCEDURE_SQL


# --- Applied at apply time, not only at translation time ---------------------------


def test_the_executor_guards_a_stale_plan():
    """A plan built before this existed must still apply cleanly, which is why the
    guard is added where the SQL runs rather than where it was produced."""
    item = PlanItem(id="procedure:dbo.usp_ItemReport", kind=ObjectKind.PROCEDURE,
                    name="public.usp_ItemReport", sql=FUNCTION_SQL)
    assert "DROP PROCEDURE" in _item_sql(item)


def test_the_executor_leaves_an_ordinary_procedure_alone():
    item = PlanItem(id="procedure:dbo.usp_SetStatus", kind=ObjectKind.PROCEDURE,
                    name="public.usp_set_status", sql=PROCEDURE_SQL)
    assert _item_sql(item) == PROCEDURE_SQL


def test_the_async_notebook_guards_too():
    from backend.data_migration.etl_generator import _post_load_rows
    from backend.data_migration.models import PostLoadStatement

    rendered = _post_load_rows([
        PostLoadStatement(name="public.usp_ItemReport", kind="procedure", sql=FUNCTION_SQL),
    ])
    assert "DROP PROCEDURE" in rendered
