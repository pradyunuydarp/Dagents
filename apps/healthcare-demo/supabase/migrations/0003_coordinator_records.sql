-- Stroke-triage demo: what the coordinator keeps across restarts, and where
-- each audit chain's check resumes.
--
-- Three tables in the `coordinator` schema, which 0002 created:
--
-- - coordinator.pilot_reports: the report of each federated pilot, so the
--   consortium charts still have data after the API restarts. The app keeps
--   the newest 20 and deletes older ones. A report holds round results, gate
--   results and the training curve. It holds no hospital's audit records and
--   no patient-level rows.
-- - coordinator.guard_audit: the audit chain of the coordinator's release
--   Guard. Its columns are the ones each hospital's guard_audit table has,
--   because dagents/common/infrastructure/postgres_audit.py writes both.
-- - coordinator.guard_audit_anchor: where the last successful check of that
--   chain ended (see below).
--
-- And one table in each hospital's schema:
--
-- - <site>.guard_audit_anchor: where the last successful check of that
--   hospital's chain ended.
--
-- An anchor table holds one row: the seq and digest of the last record a
-- successful check covered, and how many records the chain held up to it. The
-- hosted API restarts after every idle period. A new process reads the anchor,
-- hashes that record again, checks the count, and then checks only the records
-- after it, instead of every record. The anchor saves work and protects
-- nothing: the role that can change a chain can change its anchor.
--
-- Each table is owned by the role whose chain it belongs to: a hospital's
-- anchor by that hospital's role, the coordinator's tables by
-- dagents_coordinator. Row level security is on with no policy, and every
-- grant to PUBLIC and to Supabase's Data API roles is revoked, so only the
-- owning role can read them.
--
-- Run as the project's login role, after 0002: it switches to each role with
-- SET LOCAL ROLE, which 0002 granted. Running this file twice changes nothing.

do $$
declare
    -- The demo hospitals, as in 0002. Each name is both the schema and the role.
    sites       text[] := array['site_uk_134', 'site_it_002', 'site_ch_245'];
    coordinator_role constant text := 'dagents_coordinator';
    api_roles   text[] := array['anon', 'authenticated', 'service_role'];
    r           text;
    s           text;
    t           record;
    expected_owner text;
    anchors     int;
begin
    if not exists (select 1 from pg_namespace where nspname = 'coordinator') then
        raise exception 'the coordinator schema does not exist; apply 0002_site_isolation.sql first';
    end if;
    foreach s in array sites loop
        if not exists (select 1 from pg_namespace where nspname = s) then
            raise exception 'the schema % does not exist; apply 0002_site_isolation.sql first', s;
        end if;
    end loop;

    -- Each hospital's anchor, created while acting as the hospital, so the
    -- hospital owns it.
    foreach s in array sites loop
        execute format('set local role %I', s);
        execute format($ddl$
            create table if not exists %I.guard_audit_anchor (
                id           boolean primary key default true check (id),
                seq          bigint not null check (seq >= 1),
                digest       text not null check (digest ~ '^[0-9a-f]{64}$'),
                record_count bigint not null check (record_count between 1 and seq),
                verified_at  timestamptz not null default now()
            )$ddl$, s);
        execute format('alter table %I.guard_audit_anchor enable row level security', s);
        execute format('revoke all on %I.guard_audit_anchor from public', s);
        foreach r in array api_roles loop
            if exists (select 1 from pg_roles where rolname = r) then
                execute format('revoke all on %I.guard_audit_anchor from %I', s, r);
            end if;
        end loop;
        execute 'reset role';
    end loop;

    -- Created while acting as the coordinator, so the coordinator owns them.
    execute format('set local role %I', coordinator_role);

    create table if not exists coordinator.pilot_reports (
        seq        bigint generated always as identity unique,
        pilot_id   text primary key check (length(pilot_id) between 1 and 200),
        report     jsonb not null check (jsonb_typeof(report) = 'object'),
        created_at timestamptz not null default now()
    );

    -- The same columns and constraints as each hospital's guard_audit table.
    -- previous_digest is unique, so the chain cannot fork.
    create table if not exists coordinator.guard_audit (
        seq             bigint generated always as identity primary key,
        audit_id        text not null unique,
        recorded_at     bigint not null,
        boundary        text not null,
        decision        text not null,
        permitted       boolean not null,
        record          jsonb not null,
        previous_digest text not null unique,
        digest          text not null unique
    );

    -- The coordinator's anchor, with the same columns as each hospital's.
    create table if not exists coordinator.guard_audit_anchor (
        id           boolean primary key default true check (id),
        seq          bigint not null check (seq >= 1),
        digest       text not null check (digest ~ '^[0-9a-f]{64}$'),
        record_count bigint not null check (record_count between 1 and seq),
        verified_at  timestamptz not null default now()
    );

    alter table coordinator.pilot_reports      enable row level security;
    alter table coordinator.guard_audit        enable row level security;
    alter table coordinator.guard_audit_anchor enable row level security;

    revoke all on coordinator.pilot_reports      from public;
    revoke all on coordinator.guard_audit        from public;
    revoke all on coordinator.guard_audit_anchor from public;
    foreach r in array api_roles loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on coordinator.pilot_reports from %I', r);
            execute format('revoke all on coordinator.guard_audit from %I', r);
            execute format('revoke all on coordinator.guard_audit_anchor from %I', r);
            execute format('revoke all on all sequences in schema coordinator from %I', r);
        end if;
    end loop;

    execute 'reset role';

    -- Check the result, so a partial earlier attempt cannot pass unnoticed.
    for t in
        select n.nspname as schema_name, c.relname as table_name, pg_get_userbyid(c.relowner) as owner,
               c.relrowsecurity as rls
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind = 'r' and (
            (n.nspname = 'coordinator' and c.relname in ('pilot_reports', 'guard_audit', 'guard_audit_anchor'))
            or (n.nspname = any(sites) and c.relname = 'guard_audit_anchor'))
    loop
        if not t.rls then
            raise exception 'row level security is off on %.%', t.schema_name, t.table_name;
        end if;
        expected_owner := t.schema_name;
        if t.schema_name = 'coordinator' then
            expected_owner := coordinator_role;
        end if;
        if t.owner <> expected_owner then
            raise exception '%.% is owned by %, expected %', t.schema_name, t.table_name, t.owner, expected_owner;
        end if;
    end loop;
    select count(*) into anchors
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind = 'r' and c.relname = 'guard_audit_anchor' and n.nspname = any(sites || 'coordinator'::text);
    if anchors <> array_length(sites, 1) + 1 then
        raise exception 'expected % guard_audit_anchor tables, found %', array_length(sites, 1) + 1, anchors;
    end if;
end
$$;
