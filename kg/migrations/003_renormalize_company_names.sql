-- 003: bring companies.name_norm in line with kg/companies.py::_normalize_name.
--
-- Rows written before the "alphanumeric-only" step kept spaces/punctuation
-- ('52 north health' instead of '52northhealth'), so the ingest no longer
-- matches them exactly, and spacing variants were created as separate
-- companies ('3DBioFibR' + '3D BioFibR Inc'). The stored value is already
-- lower-cased and suffix-stripped, so the current rule is just that value with
-- non-alphanumerics removed.
--
-- Companies that collide under the current rule are merged into the one with
-- the lowest company_code: their appearances, sources, claims, documents and
-- aliases move to it (claims keep their own source, so nothing conflicts on
-- claims_idem and disagreeing values stay visible). Idempotent: a second run
-- finds nothing to change.

do $$
declare
  g record;
  keeper uuid;
  losers uuid[];
begin
  for g in
    select regexp_replace(name_norm, '[^a-z0-9]+', '', 'g') as norm,
           array_agg(id order by company_code, created_at) as ids
    from companies
    group by 1
    having count(*) > 1
  loop
    keeper := g.ids[1];
    losers := g.ids[2:];
    update company_appearances set company_id = keeper where company_id = any(losers);
    update sources set company_id = keeper where company_id = any(losers);
    update claims set company_id = keeper where company_id = any(losers);
    update documents set company_id = keeper where company_id = any(losers);
    update company_aliases set company_id = keeper where company_id = any(losers);
    update ingest_unmatched set candidate_company_id = keeper where candidate_company_id = any(losers);
    delete from companies where id = any(losers);
    raise notice 'merged % companies into % (%)', array_length(losers, 1), keeper, g.norm;
  end loop;

  update companies
     set name_norm = regexp_replace(name_norm, '[^a-z0-9]+', '', 'g'), updated_at = now()
   where name_norm <> regexp_replace(name_norm, '[^a-z0-9]+', '', 'g');
end $$;
