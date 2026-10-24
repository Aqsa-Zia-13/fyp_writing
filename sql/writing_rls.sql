-- Apply this migration to the Supabase project database (public schema).
-- Requests in fyp_writing assume the `authenticated` DB role and set the verified JWT sub claim.
DO $$
DECLARE
    v_table text;
    v_policy text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['writing_task1', 'writing_task2'] LOOP
        IF to_regclass(format('%I.%I', current_schema(), v_table)) IS NULL THEN
            RAISE EXCEPTION 'Required IELTS prompt table % is missing', v_table;
        END IF;
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', current_schema(), v_table);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY', current_schema(), v_table);

        v_policy := v_table || '_authenticated_read';
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', v_policy, current_schema(), v_table);
        EXECUTE format('CREATE POLICY %I ON %I.%I AS PERMISSIVE FOR SELECT TO authenticated USING (auth.uid() IS NOT NULL)', v_policy, current_schema(), v_table);

        v_policy := v_table || '_identity_guard';
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', v_policy, current_schema(), v_table);
        EXECUTE format('CREATE POLICY %I ON %I.%I AS RESTRICTIVE FOR SELECT TO PUBLIC USING (auth.uid() IS NOT NULL)', v_policy, current_schema(), v_table);
    END LOOP;

    FOREACH v_table IN ARRAY ARRAY['writing_response1', 'writing_response2', 'writing_score1', 'writing_score2'] LOOP
        IF to_regclass(format('%I.%I', current_schema(), v_table)) IS NULL THEN
            RAISE EXCEPTION 'Required IELTS response/score table % is missing', v_table;
        END IF;
        EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS user_id text', current_schema(), v_table);
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', current_schema(), v_table);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY', current_schema(), v_table);

        v_policy := v_table || '_user_access';
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', v_policy, current_schema(), v_table);
        EXECUTE format(
            'CREATE POLICY %I ON %I.%I AS PERMISSIVE FOR ALL TO authenticated USING (auth.uid() IS NOT NULL AND user_id::text = auth.uid()::text) WITH CHECK (auth.uid() IS NOT NULL AND user_id::text = auth.uid()::text)',
            v_policy, current_schema(), v_table
        );

        v_policy := v_table || '_identity_guard';
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', v_policy, current_schema(), v_table);
        EXECUTE format(
            'CREATE POLICY %I ON %I.%I AS RESTRICTIVE FOR ALL TO PUBLIC USING (auth.uid() IS NOT NULL AND user_id::text = auth.uid()::text) WITH CHECK (auth.uid() IS NOT NULL AND user_id::text = auth.uid()::text)',
            v_policy, current_schema(), v_table
        );
    END LOOP;
END $$;