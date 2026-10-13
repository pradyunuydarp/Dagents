-- For tests and CI only. Never run this against a Supabase project.
--
-- Builds the parts of a Supabase project that matter for site isolation on a
-- plain PostgreSQL 16, so the migrations can be tested as Supabase runs them.
-- Run it as a superuser, connected to a scratch database.
--
-- `supa_like` stands in for Supabase's `postgres` role, which is not a
-- superuser. The attributes come from supabase/postgres:
--   migrations/db/migrations/10000000000000_demote-postgres.sql
--     ALTER ROLE postgres NOSUPERUSER CREATEDB CREATEROLE LOGIN REPLICATION BYPASSRLS;
--   migrations/db/init-scripts/00000000000000-initial-schema.sql
--     (anon, authenticated, service_role)
--   migrations/db/migrations/20230201083204_grant_auth_roles_to_postgres.sql
--
-- Not emulated: Supabase also grants `pg_read_all_data` to `postgres`. With it,
-- the connecting role can read every schema without switching role. The
-- isolation tests grant it in one test to show that the site roles stay
-- isolated, and revoke it afterwards.
--
-- The script sets no password. The test harness sets one derived from its admin
-- connection string, the same for every run against one server, so suites that
-- run at the same time do not lock each other out.
-- Roles are shared by the whole cluster, so the script creates each role only
-- if it is missing; running it twice changes nothing.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'supa_like') then
        create role supa_like login nosuperuser createdb createrole bypassrls;
    else
        alter role supa_like login nosuperuser createdb createrole bypassrls;
    end if;

    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin noinherit bypassrls;
    end if;
end
$$;

grant anon, authenticated, service_role to supa_like;

do $$
begin
    execute format('grant all on database %I to supa_like', current_database());
end
$$;

-- Supabase's posture for the public schema: the API roles may use it, and new
-- tables the connecting role creates there are granted to them by default.
grant usage on schema public to anon, authenticated, service_role;
grant create on schema public to supa_like;
alter default privileges for role supa_like in schema public
    grant all on tables to anon, authenticated, service_role;
