-- Stroke-triage demo: hospitals and encounters.
--
-- The column list is not a design decision made here. It is
-- `stroke-triage-features-v2`, the feature contract in
-- `backend/app/domain/conditions.py`, reproduced one column per field — because
-- a consortium's whole point is that every site derives the same fields with
-- the same units, and a schema that drifted from the contract would silently
-- break that agreement rather than fail.
--
-- Two properties are deliberate and easy to undo by accident:
--
--   * Required contract fields are NULLABLE here. Real records have gaps, and
--     the demo's data-quality path exists to catch exactly that. A NOT NULL
--     constraint would move the check from the framework to the database and
--     make the missingness path unreachable.
--   * `confirmed_stroke` is the retrospective outcome label. It is in the
--     contract because an evaluation round cannot measure anything without it,
--     and a live or training round must never read it. Nothing in the schema can
--     enforce that; the Ethical Guard does, at `before_read` and `before_train`.
--
-- Every row this table will ever hold is synthetic. That is a constraint, not a
-- convenience: a demonstration of privacy controls carrying real patient data
-- would be self-refuting.

create table if not exists public.hospitals (
    site_id          text primary key,
    display_name     text        not null,
    -- Generator parameters, kept so a reseed reproduces the same consortium and
    -- so the per-site differences are inspectable rather than folk knowledge.
    seed             integer     not null,
    stroke_rate      real        not null,
    severity_bias    real        not null default 0,
    missingness      real        not null default 0.05,
    older_population boolean     not null default false,
    glucose_unit_error_rate real not null default 0,
    created_at       timestamptz not null default now()
);

comment on table public.hospitals is
    'The simulated consortium. Sites differ on purpose: identical sites would never exercise the drift checks, the cohort floors, or the fairness gate.';

create table if not exists public.encounters (
    encounter_id             text primary key,
    site_id                  text not null references public.hospitals (site_id) on delete cascade,

    -- stroke-triage-features-v2, required fields
    nihss_total              real,        -- points, NIHSS, 0-42
    face_droop               boolean,     -- FAST
    arm_weakness             boolean,     -- FAST
    speech_difficulty        boolean,     -- FAST
    last_known_well_minutes  real,        -- minutes
    age_band                 text,        -- bands rather than ages, by design

    -- stroke-triage-features-v2, optional fields
    systolic_bp              real,        -- mmHg
    blood_glucose            real,        -- mmol/L
    blood_glucose_unit       text,        -- set only when a site reported mg/dL without saying so
    arrival_mode             text,
    anticoagulated           boolean,
    confirmed_stroke         boolean,     -- retrospective label; never read by a live or training round

    recorded_at              timestamptz not null default now()
);

comment on column public.encounters.blood_glucose_unit is
    'Present only for the seeded unit-error rows. The FHIR ingestion mapper catches this when a unit travels with the value; left in so the downstream view of an unconverted value is visible.';

comment on column public.encounters.confirmed_stroke is
    'Retrospective outcome label. In the contract so an evaluation round can measure something; a live or training round must never read it. Enforced by the Ethical Guard, not by this schema.';

-- The worklist reads one site at a time, ordered by severity.
create index if not exists encounters_site_idx on public.encounters (site_id);
create index if not exists encounters_site_nihss_idx on public.encounters (site_id, nihss_total desc nulls last);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
--
-- RLS is enabled and NO policy is created, which means: with the publishable
-- (anon) key these tables return zero rows. That is the intended posture. The
-- browser never talks to Supabase — the API service does, with the service-role
-- key, and the Ethical Guard decides what leaves. Adding an anon read policy
-- here would route patient-level rows straight past the Guard to the browser
-- and defeat the entire demo.
--
-- The service role bypasses RLS by design, so the API is unaffected.

alter table public.hospitals  enable row level security;
alter table public.encounters enable row level security;

-- `anon` and `authenticated` are Supabase's roles; a plain Postgres — a local
-- one for development, or the throwaway instance CI applies this against — has
-- neither. Revoking unconditionally would make this migration run on Supabase
-- and fail everywhere else, which is the wrong way round: the place it must be
-- possible to rehearse is the place it would break.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.hospitals  from anon;
        revoke all on public.encounters from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.hospitals  from authenticated;
        revoke all on public.encounters from authenticated;
    end if;
end
$$;
