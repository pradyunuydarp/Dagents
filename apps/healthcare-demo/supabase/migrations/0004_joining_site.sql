-- Stroke-triage demo: a schema and a role for each hospital that joins while
-- the demo runs.
--
-- Hospital 290 (`site_pl_290`) is not in the consortium when the demo starts.
-- A network-mode run started with --deploy adds it: the app starts its LMA,
-- and the GMA accepts its address and enrols it. Its LMA reads its records as
-- its own role and keeps its audit chain and job log there, so it needs what
-- 0002 and 0003 give every other hospital:
--
-- - a NOLOGIN role and a schema with the same name, owned by the role;
-- - the `encounters` and `guard_audit` tables of 0002 and the
--   `guard_audit_anchor` table of 0003, owned by the role, with row level
--   security on and no policy;
-- - every grant to PUBLIC and to Supabase's Data API roles revoked;
-- - the grants on `coordinator.site_contributions` (the LMA job log) that 0002
--   gives each hospital, and the role named in that table's two policies, so
--   it inserts and reads only its own rows.
--
-- The coordinator role gets no grant on the joining hospital's schema.
--
-- The hospital's records are seeded with the others (scripts/seed_supabase.py).
-- The hosted API never adds a hospital, so on the hosted database the schema is
-- seeded and never read.
--
-- Run as the project's login role, after 0003: it switches to each role with
-- SET LOCAL ROLE. Running this file twice changes nothing. On PostgreSQL 15
-- the role grant is a plain GRANT, as in 0002, which says what that gives up.

do $$
declare
    -- The hospitals that join while the demo runs. Each name is both the schema and the role.
    sites       text[] := array['site_pl_290'];
    -- Every hospital: 0002's and the ones above. ALTER POLICY ... TO replaces
    -- a policy's whole role list, so it names them all.
    all_sites   text[] := array['site_uk_134', 'site_it_002', 'site_ch_245', 'site_pl_290'];
    coordinator_role constant text := 'dagents_coordinator';
    api_roles   text[] := array['anon', 'authenticated', 'service_role'];
    site_list   text;
    all_list    text;
    r           text;
    s           text;
    t           record;
    -- Per-grant INHERIT and SET options exist from PostgreSQL 16.
    grant_options constant boolean := current_setting('server_version_num')::int >= 160000;
begin
    -- Read the catalog, not to_regclass: the login role has no usage on the
    -- coordinator schema.
    if (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'coordinator' and c.relname in ('pilot_reports', 'site_contributions')) <> 2 then
        raise exception 'the coordinator tables do not exist; apply 0003_coordinator_records.sql first';
    end if;
    site_list := array_to_string(array(select quote_ident(x) from unnest(sites) as x), ', ');
    all_list := array_to_string(array(select quote_ident(x) from unnest(all_sites) as x), ', ');

    -- 1. Roles, as in 0002.
    foreach r in array sites loop
        if not exists (select 1 from pg_roles where rolname = r) then
            execute format('create role %I nologin noinherit nobypassrls', r);
        end if;
        if grant_options then
            execute format('grant %I to current_user with inherit false, set true', r);
        else
            execute format('grant %I to current_user', r);
        end if;
    end loop;

    -- 2. One schema per hospital, with the tables of 0002 and 0003, created
    --    while acting as the hospital's role, so the hospital owns them.
    foreach s in array sites loop
        execute format('create schema if not exists %I authorization %I', s, s);
        execute format('set local role %I', s);

        execute format($ddl$
            create table if not exists %I.encounters (
                encounter_id        text primary key
                                    check (encounter_id ~ '^[a-z0-9-]+-(e[1-9][0-9]*|[0-9]{5,})$'),
                source_row          integer unique check (source_row >= 1),
                origin              text not null default 'dataset'
                                    check (origin in ('dataset', 'entered')),
                age                 integer check (age between 16 and 110),
                age_band            text check (age_band in ('<50', '50-59', '60-69', '70-79', '80-89', '90+')),
                sex                 text check (sex in ('female', 'male')),
                conscious_level     text check (conscious_level in ('alert', 'drowsy', 'unconscious')),
                face_weakness       boolean,
                arm_weakness        boolean,
                leg_weakness        boolean,
                speech_difficulty   boolean,
                hours_since_onset   double precision check (hours_since_onset between 0 and 48),
                systolic_bp         double precision check (systolic_bp between 60 and 300),
                visual_field_loss   boolean,
                atrial_fibrillation boolean,
                onset_on_waking     boolean,
                stroke_subtype      text check (stroke_subtype in ('TACS', 'PACS', 'LACS', 'POCS', 'other')),
                poor_outcome_6m     boolean,
                recorded_at         timestamptz not null default now(),
                outcome_recorded_at timestamptz,
                client_request_id   text unique check (length(client_request_id) between 1 and 200),
                constraint encounters_origin_matches_row check (
                    (origin = 'dataset' and source_row is not null
                        and encounter_id !~ '-e[0-9]+$'
                        and client_request_id is null and outcome_recorded_at is null)
                    or (origin = 'entered' and source_row is null
                        and encounter_id ~ '-e[1-9][0-9]*$')
                ),
                constraint encounters_age_band_matches_age check (
                    age is null or age_band is null or age_band = case
                        when age < 50 then '<50'
                        when age < 60 then '50-59'
                        when age < 70 then '60-69'
                        when age < 80 then '70-79'
                        when age < 90 then '80-89'
                        else '90+'
                    end
                )
            )$ddl$, s);

        execute format($ddl$
            create table if not exists %I.guard_audit (
                seq             bigint generated always as identity primary key,
                audit_id        text not null unique,
                recorded_at     bigint not null,
                boundary        text not null,
                decision        text not null,
                permitted       boolean not null,
                record          jsonb not null,
                previous_digest text not null unique,
                digest          text not null unique
            )$ddl$, s);

        execute format($ddl$
            create table if not exists %I.guard_audit_anchor (
                id           boolean primary key default true check (id),
                seq          bigint not null check (seq >= 1),
                digest       text not null check (digest ~ '^[0-9a-f]{64}$'),
                record_count bigint not null check (record_count between 1 and seq),
                verified_at  timestamptz not null default now()
            )$ddl$, s);

        execute format('alter table %I.encounters enable row level security', s);
        execute format('alter table %I.guard_audit enable row level security', s);
        execute format('alter table %I.guard_audit_anchor enable row level security', s);

        execute format('revoke all on schema %I from public', s);
        execute format('revoke all on all tables in schema %I from public', s);
        execute 'alter default privileges revoke execute on functions from public';
        foreach r in array api_roles loop
            if exists (select 1 from pg_roles where rolname = r) then
                execute format('revoke all on schema %I from %I', s, r);
                execute format('revoke all on all tables in schema %I from %I', s, r);
                execute format('revoke all on all sequences in schema %I from %I', s, r);
            end if;
        end loop;

        execute 'reset role';
    end loop;

    -- 3. The LMA job log, coordinator.site_contributions: the grants 0002
    --    gives each hospital, and every hospital in the two policies.
    execute format('set local role %I', coordinator_role);
    execute format('grant usage on schema coordinator to %s', site_list);
    execute format('grant select on coordinator.site_contributions to %s', site_list);
    execute format(
        'grant insert (round_id, phase, payload) on coordinator.site_contributions to %s',
        site_list
    );
    execute format(
        'alter policy site_contributions_insert_own on coordinator.site_contributions to %s',
        all_list
    );
    execute format(
        'alter policy site_contributions_select_own on coordinator.site_contributions to %s',
        all_list
    );
    execute 'reset role';

    -- 4. Check the result, so a partial earlier attempt cannot pass unnoticed.
    for t in
        select n.nspname as schema_name, c.relname as table_name,
               pg_get_userbyid(c.relowner) as owner, c.relrowsecurity as rls,
               pg_get_userbyid(n.nspowner) as schema_owner
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r', 'p') and n.nspname = any (sites)
    loop
        if not t.rls then
            raise exception 'row level security is off on %.%', t.schema_name, t.table_name;
        end if;
        if t.owner <> t.schema_name or t.schema_owner <> t.schema_name then
            raise exception '%.% is owned by %, schema by %, expected %',
                t.schema_name, t.table_name, t.owner, t.schema_owner, t.schema_name;
        end if;
    end loop;
    foreach s in array sites loop
        if (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where c.relkind = 'r' and n.nspname = s) <> 3 then
            raise exception 'expected 3 tables in schema %', s;
        end if;
    end loop;
end
$$;
