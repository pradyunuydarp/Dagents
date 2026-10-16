-- Stroke-triage demo: one schema and one role per hospital.
--
-- Each demo hospital gets a schema and a NOLOGIN role with the same name. The
-- role owns the schema and its tables, so a hospital's records and its audit
-- chain are readable and writable only as that hospital's role. A coordinator
-- role owns the `coordinator` schema (the model registry and what hospitals
-- publish) and has no access to any hospital schema.
--
-- Who connects: the API and the data workflow connect as the project's login
-- role (`postgres` on Supabase; no Supabase API key is used). That role creates
-- the roles below and is granted each one. On PostgreSQL 16 or later the grant
-- is WITH INHERIT FALSE, SET TRUE: the login may switch to a role with SET
-- LOCAL ROLE, and holds none of the role's privileges until it does. Every
-- database operation in the app runs in one transaction that starts with
-- SET LOCAL ROLE.
--
-- PostgreSQL 15 has no per-grant options, so there the grant is a plain GRANT
-- and the login inherits every site role's privileges. It can then read each
-- hospital's tables without switching role. Step 1 says more.
--
-- Limit: on Supabase the `postgres` role also has BYPASSRLS and is a member of
-- pg_read_all_data, so that credential can read every schema directly on any
-- version. The isolation therefore applies to the app's code, which always acts
-- as a site or coordinator role. It does not restrict the credential. A real
-- deployment gives each hospital's agent its own login role and database.
--
-- Run as the project's login role. It needs CREATEROLE and PostgreSQL 15 or
-- later. Running this file twice changes nothing, and running it on a database
-- where an earlier version of this file ran adds what that version lacked.
--
-- The encounter columns are `stroke-triage-features-v3` plus the row key, the
-- exact age (which stays at the hospital) and bookkeeping columns. Contract
-- fields are nullable: real records have gaps, and the framework's data-quality
-- checks must be able to see them.

-- 0. Refuse servers older than PostgreSQL 15.
do $$
begin
    if current_setting('server_version_num')::int < 150000 then
        raise exception 'site isolation needs PostgreSQL 15 or later, found %',
            current_setting('server_version');
    end if;
end
$$;

do $$
declare
    -- The demo hospitals. Each name is both the schema and the role.
    sites       text[] := array['site_uk_134', 'site_it_002', 'site_ch_245'];
    coordinator_role constant text := 'dagents_coordinator';
    api_roles   text[] := array['anon', 'authenticated', 'service_role'];
    site_list   text;
    r           text;
    s           text;
    t           record;
    expected_owner text;
    -- Per-grant INHERIT and SET options exist from PostgreSQL 16.
    grant_options constant boolean := current_setting('server_version_num')::int >= 160000;
begin
    site_list := array_to_string(array(select quote_ident(x) from unnest(sites) as x), ', ');

    -- 1. Roles. NOLOGIN: nobody connects as a site. NOBYPASSRLS: row level
    --    security applies to them.
    --
    --    On PostgreSQL 16 or later the grant gives the connecting role SET (it
    --    may switch to the role) without INHERIT (it holds none of the role's
    --    rights).
    --
    --    On PostgreSQL 15 the grant is plain: the connecting role may switch to
    --    the role and also inherits its rights, so it can read and write every
    --    hospital's tables without switching. What is lost is that the
    --    credential itself is no longer restricted; the isolation binds only
    --    the code paths, which always switch role. On Supabase that is already
    --    the case on any version, because its `postgres` login holds
    --    pg_read_all_data. The site roles stay isolated from each other and
    --    from the coordinator on both versions.
    foreach r in array sites || coordinator_role loop
        if not exists (select 1 from pg_roles where rolname = r) then
            execute format('create role %I nologin noinherit nobypassrls', r);
        end if;
        if grant_options then
            execute format('grant %I to current_user with inherit false, set true', r);
        else
            execute format('grant %I to current_user', r);
        end if;
    end loop;

    -- 2. One schema per hospital. Tables are created while acting as the
    --    hospital's role, so the hospital owns them and the connecting role
    --    does not.
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
                -- Dataset rows come from the IST extract: they have a source row
                -- and no entry bookkeeping. Entered rows have neither a source
                -- row nor a dataset-style id.
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

        -- The hospital's own audit chain. The columns are the ones
        -- dagents/common/infrastructure/postgres_audit.py reads and writes.
        -- previous_digest is unique, so the chain cannot fork.
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

        -- RLS on with no policy: the owner (the hospital's role) is exempt, and
        -- any other role without BYPASSRLS sees no rows even if a grant is
        -- added by mistake.
        execute format('alter table %I.encounters enable row level security', s);
        execute format('alter table %I.guard_audit enable row level security', s);

        execute format('revoke all on schema %I from public', s);
        execute format('revoke all on all tables in schema %I from public', s);
        -- Role-wide on purpose: a per-schema REVOKE cannot remove the built-in
        -- EXECUTE that PUBLIC has on new functions.
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

    -- 3. The coordinator schema: the model registry and what hospitals publish.
    execute format('create schema if not exists coordinator authorization %I', coordinator_role);
    execute format('set local role %I', coordinator_role);

    create table if not exists coordinator.models (
        version        text primary key check (length(version) between 1 and 200),
        kind           text not null check (length(kind) between 1 and 64),
        params         jsonb not null,
        digest         text not null check (length(digest) between 1 and 200),
        parent_version text references coordinator.models (version),
        created_at     timestamptz not null default now(),
        status         text not null
                       check (status in ('candidate', 'released', 'rejected', 'superseded'))
    );

    -- release_decision is the latest release decision for the version:
    -- {action, guard_permitted, gate_results, round_id}. decided_at is when it
    -- was stored. An approval reads both from here, so it still works after
    -- the API restarts.
    --
    -- They are added here rather than in the create table above, so a database
    -- where an earlier version of this file created the table gets them too.
    -- The constraint names are the ones an inline definition produced, so a
    -- database that already has them keeps them.
    alter table coordinator.models add column if not exists release_decision jsonb;
    alter table coordinator.models add column if not exists decided_at timestamptz;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'coordinator.models'::regclass and conname = 'models_release_decision_check'
    ) then
        alter table coordinator.models add constraint models_release_decision_check
            check (release_decision is null or (
                jsonb_typeof(release_decision) = 'object'
                and jsonb_typeof(release_decision -> 'action') = 'string'
                and jsonb_typeof(release_decision -> 'guard_permitted') = 'boolean'
                and jsonb_typeof(release_decision -> 'gate_results') = 'array'
            ));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'coordinator.models'::regclass and conname = 'models_decision_has_time'
    ) then
        alter table coordinator.models add constraint models_decision_has_time
            check ((release_decision is null) = (decided_at is null));
    end if;
    -- At most one model is the release the hospitals use.
    create unique index if not exists models_one_release on coordinator.models ((true))
        where status = 'released';

    -- The one table hospitals write to. A hospital inserts and reads only rows
    -- that carry its own role name; the coordinator, as owner, reads them all.
    create table if not exists coordinator.site_contributions (
        contribution_id bigint generated always as identity primary key,
        round_id        text not null check (length(round_id) between 1 and 200),
        phase           text not null check (length(phase) between 1 and 64),
        site_role       name not null default current_user,
        payload         jsonb not null,
        submitted_at    timestamptz not null default now()
    );

    alter table coordinator.models             enable row level security;
    alter table coordinator.site_contributions enable row level security;

    execute format('grant usage on schema coordinator to %s', site_list);
    execute format('grant select on coordinator.site_contributions to %s', site_list);
    -- Column grants: a hospital cannot set its own site_role, id or timestamp.
    execute format(
        'grant insert (round_id, phase, payload) on coordinator.site_contributions to %s',
        site_list
    );

    drop policy if exists site_contributions_insert_own on coordinator.site_contributions;
    execute format(
        'create policy site_contributions_insert_own on coordinator.site_contributions '
        'for insert to %s with check (site_role = current_user)',
        site_list
    );
    drop policy if exists site_contributions_select_own on coordinator.site_contributions;
    execute format(
        'create policy site_contributions_select_own on coordinator.site_contributions '
        'for select to %s using (site_role = current_user)',
        site_list
    );

    revoke all on schema coordinator from public;
    revoke all on all tables in schema coordinator from public;
    alter default privileges revoke execute on functions from public;
    foreach r in array api_roles loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on schema coordinator from %I', r);
            execute format('revoke all on all tables in schema coordinator from %I', r);
            execute format('revoke all on all sequences in schema coordinator from %I', r);
        end if;
    end loop;

    execute 'reset role';

    -- 4. Check the result, so a partial earlier attempt cannot pass unnoticed.
    for t in
        select n.nspname as schema_name, c.relname as table_name,
               pg_get_userbyid(c.relowner) as owner, c.relrowsecurity as rls,
               pg_get_userbyid(n.nspowner) as schema_owner
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r', 'p') and n.nspname = any (sites || array['coordinator'])
    loop
        if not t.rls then
            raise exception 'row level security is off on %.%', t.schema_name, t.table_name;
        end if;
        expected_owner := coalesce(nullif(t.schema_name::text, 'coordinator'), coordinator_role);
        if t.owner <> expected_owner or t.schema_owner <> expected_owner then
            raise exception '%.% is owned by %, schema by %, expected %',
                t.schema_name, t.table_name, t.owner, t.schema_owner, expected_owner;
        end if;
    end loop;
end
$$;

-- 5. The synthetic tables from 0001 (public.encounters and public.hospitals)
--    stay. The previous version of the API reads them, and it keeps serving
--    while this migration and the seed run, until the new version is
--    deployed. This version never reads them. A later migration drops them.
