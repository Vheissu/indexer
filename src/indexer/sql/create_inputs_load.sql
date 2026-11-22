-- Where upsert_inputs.sql reads the loaded rows from (filled by COPY).
CREATE TEMP TABLE inputs_load (url text, source text, source_license text) ON COMMIT DROP
