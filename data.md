## 各 parquet 仓库行数

| 文件 | 行数 |
| --- | ---: |
| sciscinet_author_details.parquet | 100,418,971 |
| sciscinet_authors.parquet | 100,418,971 |
| sciscinet_authors_paperid.parquet | 772,984,433 |
| sciscinet_fields.parquet | 303 |
| sciscinet_paper_author_affiliation.parquet | 772,984,433 |
| sciscinet_paperfields.parquet | 1,271,891,157 |

## EU

┌─────────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│     column_name     │ column_type │  null   │   key   │ default │  extra  │
│       varchar       │   varchar   │ varchar │ varchar │ varchar │ varchar │
├─────────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ Programme           │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Acronym             │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Project Title       │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Abstract            │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Researcher(s)       │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Host Institution(s) │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Country             │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Region              │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Project Number      │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Call                │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Grant Type          │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Domain              │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Panel               │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Call Year           │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ Start Date          │ DATE        │ YES     │ NULL    │ NULL    │ NULL    │
│ End Date            │ DATE        │ YES     │ NULL    │ NULL    │ NULL    │
│ EU contribution     │ DOUBLE      │ YES     │ NULL    │ NULL    │ NULL    │
│ CORDIS Link         │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
└─────────────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘


## sciscinet_authors_paperid.parquet



┌──────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│ column_name  │ column_type │  null   │   key   │ default │  extra  │
│   varchar    │   varchar   │ varchar │ varchar │ varchar │ varchar │
├──────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ authorid     │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ display_name │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ paperid      │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
└──────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘

## sciscinet_author_details.parquet



┌───────────────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│        column_name        │ column_type │  null   │   key   │ default │  extra  │
│          varchar          │   varchar   │ varchar │ varchar │ varchar │ varchar │
├───────────────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ authorid                  │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ orcid                     │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ display_name              │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ display_name_alternatives │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ works_count               │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ cited_by_count            │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ last_known_institution    │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ works_api_url             │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ updated_date              │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
└───────────────────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘

## sciscinet_authors.parquet

┌───────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│    column_name    │ column_type │  null   │   key   │ default │  extra  │
│      varchar      │   varchar   │ varchar │ varchar │ varchar │ varchar │
├───────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ authorid          │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ avg_c10           │ DOUBLE      │ YES     │ NULL    │ NULL    │ NULL    │
│ avg_logc10        │ DOUBLE      │ YES     │ NULL    │ NULL    │ NULL    │
│ productivity      │ UINTEGER    │ YES     │ NULL    │ NULL    │ NULL    │
│ h_index           │ UINTEGER    │ YES     │ NULL    │ NULL    │ NULL    │
│ display_name      │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ inference_sources │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ inference_counts  │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ P(gf)             │ DOUBLE      │ YES     │ NULL    │ NULL    │ NULL    │
└───────────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘

## sciscinet_fields.parquet

┌─────────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│     column_name     │ column_type │  null   │   key   │ default │  extra  │
│       varchar       │   varchar   │ varchar │ varchar │ varchar │ varchar │
├─────────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ id                  │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ wikidata            │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ display_name        │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ level               │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ description         │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ works_count         │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ cited_by_count      │ BIGINT      │ YES     │ NULL    │ NULL    │ NULL    │
│ image_url           │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ image_thumbnail_url │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ works_api_url       │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ updated_date        │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ fieldid             │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
________________________________________________________________________________

## sciscinet_paper_author_affiliation.parquet



┌────────────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│      column_name       │ column_type │  null   │   key   │ default │  extra  │
│        varchar         │   varchar   │ varchar │ varchar │ varchar │ varchar │
├────────────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ paperid                │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ author_position        │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ authorid               │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ institutionid          │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ raw_affiliation_string │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
└────────────────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘

## sciscinet_paperfields.parquet

┌────────────────┬─────────────┬─────────┬─────────┬─────────┬─────────┐
│  column_name   │ column_type │  null   │   key   │ default │  extra  │
│    varchar     │   varchar   │ varchar │ varchar │ varchar │ varchar │
├────────────────┼─────────────┼─────────┼─────────┼─────────┼─────────┤
│ paperid        │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ fieldid        │ VARCHAR     │ YES     │ NULL    │ NULL    │ NULL    │
│ score_openalex │ DOUBLE      │ YES     │ NULL    │ NULL    │ NULL    │
└────────────────┴─────────────┴─────────┴─────────┴─────────┴─────────┘