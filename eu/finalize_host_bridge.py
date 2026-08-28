# -*- coding: utf-8 -*-
import json
import re
import shutil
from pathlib import Path

import pandas as pd

EU = "EU.xlsx"
BRIDGE = "host_ids_bridge.csv"
AUDIT = "host_ids_manual_resolved.csv"
BACKUP = "host_ids_bridge.backup_before_manual.csv"

# name+cc -> (openalex_id, matched_name, ror)
OVERRIDES = {
    ("Group of National Schools of Economics and Statistics (GENES)", "FR"): ("I4210108488", "Groupe des Écoles Nationales d'Économie et Statistique", "https://ror.org/01p6yxw64"),
    ("Academic Medical Center of the University of Amsterdam", "NL"): ("I2802928900", "Amsterdam UMC Location University of Amsterdam", "https://ror.org/03t4gr691"),
    ("Research and Higher-Education Pole University of Bordeaux", "FR"): ("I15057530", "Université de Bordeaux", "https://ror.org/057qpr032"),
    ("Free University and Medical Center Amsterdam (VU-VUmc)", "NL"): ("I865915315", "Vrije Universiteit Amsterdam", "https://ror.org/008xxew50"),
    ("BIOPOLIS (Portugal)", "PT"): ("I4405252717", "Associação BIOPOLIS - Rede de Investigação em Biodiversidade e Biologia Evolutiva", "https://ror.org/0020c4y69"),
    ("CIIMAR Interdisciplinary Centre of Marine and Environmental Research", "PT"): ("I4387153601", "Centro Interdisciplinar de Investigação Marinha e Ambiental", "https://ror.org/05p7z7s64"),
    ("Centers for Advanced Studies in the Humanities", "DE"): ("I4210126132", "Geisteswissenschaftliche Zentren Berlin", "https://ror.org/02pvadn84"),
    ("Centre for Demographic Studies Barcelona", "ES"): ("I4210121658", "Centre for Demographic Studies", "https://ror.org/02dm87055"),
    ("FCiencias.ID", "PT"): ("I4392738271", "FCiências.ID - Associação para a Investigação e Desenvolvimento de Ciências", "https://ror.org/034d74a71"),
    ("Free University Medical Center Amsterdam (VUmc)", "NL"): ("I911458345", "Amsterdam UMC Location Vrije Universiteit Amsterdam", "https://ror.org/00q6h8f30"),
    ("Higher Technical Institute for Research and Development (IST-ID)", "PT"): ("I4392738108", "IST-ID - Associação do Instituto Superior Técnico para a Investigação e Desenvolvimento", "https://ror.org/018qmvm63"),
    ("IMDEA Networks Institute (Madrid Institute for Advanced Studies of Networks)", "ES"): ("I2802499160", "IMDEA Networks", "https://ror.org/04mm9fg30"),
    ("Institute for Research in Biomedicine -  Bellinzona", "CH"): ("I4387152247", "Institute for Research in Biomedicine", "https://ror.org/05gfswd81"),
    ("Leibniz Institute for the Analysis of Biodiversity Change (LIB)", "DE"): ("I4387155279", "Leibniz Institute for the Analysis of Biodiversity Change", "https://ror.org/03k5bhd83"),
    ("National Institute of Geographic and Forest Information (IGN)", "FR"): ("I1327553481", "Institut national de l’information géographique et forestière", "https://ror.org/05jxfge78"),
    ("Natural History Museum Berlin - Leibniz Institute for Evolution and Biodiversity Science", "DE"): ("I1313606977", "Museum für Naturkunde", "https://ror.org/052d1a351"),
    ("New University of Lisbon - Association for Innovation and Development of FST (NOVA ID FCT)", "PT"): ("I4405253347", "Universidade Nova de Lisboa Associação para a Inovação e Desenvolvimento da FCT", "https://ror.org/00js76g45"),
    ("Princess Maxima Centre for Pediatric Oncology", "NL"): ("I4210127118", "Princess Máxima Center", "https://ror.org/02aj7yc53"),
    ("Research Centre of the Slovenian Academy of Sciences and Arts (ZRC SAZU)", "SI"): ("I4387154574", "Research Centre of the Slovenian Academy of Sciences and Arts", "https://ror.org/04zvj8c18"),
    ("Ri.MED Foundation", "IT"): ("I4210150906", "Ri.MED", "https://ror.org/05qetrn02"),
}

# row-level overrides for Unknown Name / blank-host rows (identified via CORDIS page).
ROW_OVERRIDES = {
    985: (None, "Institute of Biodiversity Research and Action for Nature Climate and Humanity Foundation", None, "unresolved_no_openalex"),
    1417: ("I2802487185", "Université du littoral côte d'opale", "https://ror.org/02gdcg342", "cordis_reverse"),
    1636: ("I118905719", "University of Primorska", "https://ror.org/05xefg082", "cordis_reverse"),
    2369: ("I4210156054", "Athena Research and Innovation Center In Information Communication & Knowledge Technologies", "https://ror.org/0576by029", "cordis_reverse"),
    3407: ("I3133182661", "Institute for Social Research", "https://ror.org/03egy0233", "cordis_reverse"),
    3906: ("I4210131093", "Leibniz Institute for East and Southeast European Studies", "https://ror.org/039s64n79", "cordis_reverse"),
    3910: ("I4210131266", "ELTE Research Centre for the Humanities", "https://ror.org/03wxxbs05", "cordis_reverse"),
    3979: ("I4210131266", "ELTE Research Centre for the Humanities", "https://ror.org/03wxxbs05", "cordis_reverse"),
    3980: ("I4210131266", "ELTE Research Centre for the Humanities", "https://ror.org/03wxxbs05", "cordis_reverse"),
    4163: ("I887064364", "University of Amsterdam", "https://ror.org/04dkp9463", "cordis_reverse"),
    4179: ("I145847075", "TU Wien", "https://ror.org/04d836q62", "cordis_reverse"),
    4205: ("I32597200", "Ghent University", "https://ror.org/00cv9y106", "cordis_reverse"),
    4239: ("I145847075", "TU Wien", "https://ror.org/04d836q62", "cordis_reverse"),
    4240: ("I887064364", "University of Amsterdam", "https://ror.org/04dkp9463", "cordis_reverse"),
    4241: ("I32597200", "Ghent University", "https://ror.org/00cv9y106", "cordis_reverse"),
}


def parse_host(raw):
    if not isinstance(raw, str):
        return None, None
    m = re.search(r"\[\d+,([A-Z]{2})\]", raw)
    if m:
        cc = m.group(1)
        name = raw[:m.start()].strip().rstrip(",").strip()
    else:
        cc, name = None, raw.strip()
    return name or None, cc


def main():
    eu = pd.read_excel(EU)
    bridge = pd.read_csv(BRIDGE)
    cache = json.loads(Path("manual_institution_cache.json").read_text(encoding="utf-8"))

    missing_mask = bridge["host_openalex_id"].isna()
    missing_rows = bridge.index[missing_mask].tolist()
    print("missing before:", len(missing_rows))
    print("total bridge rows:", len(bridge))

    # snapshot: only null cells may change
    before = bridge.copy()
    shutil.copyfile(BRIDGE, BACKUP)

    audit = []
    for idx in missing_rows:
        row_id = int(bridge.at[idx, "row_id"])
        raw = eu.at[row_id, "Host Institution(s)"]
        name, cc = parse_host(raw)

        if row_id in ROW_OVERRIDES:
            oid, matched, ror, method = ROW_OVERRIDES[row_id]
            bridge.at[idx, "host_openalex_id"] = oid
            audit.append({
                "row_id": row_id,
                "inst_name": name,
                "cc": cc,
                "method": method,
                "host_openalex_id": oid,
                "matched_name": matched,
                "ror": ror,
            })
            continue

        key = (name or "") + "|||" + (cc or "")
        ov = OVERRIDES.get((name, cc))
        if ov:
            oid, matched, ror = ov
            bridge.at[idx, "host_openalex_id"] = oid
            audit.append({
                "row_id": row_id,
                "inst_name": name,
                "cc": cc,
                "method": "manual_override",
                "host_openalex_id": oid,
                "matched_name": matched,
                "ror": ror,
            })
            continue

        entry = cache.get(key)
        results = (entry or {}).get("results") or []
        if results:
            top = results[0]
            oid = top.get("id")
            matched = top.get("display_name")
            ror = top.get("ror")
            bridge.at[idx, "host_openalex_id"] = oid
            audit.append({
                "row_id": row_id,
                "inst_name": name,
                "cc": cc,
                "method": "api_top",
                "host_openalex_id": oid,
                "matched_name": matched,
                "ror": ror,
            })
            continue

        audit.append({
            "row_id": row_id,
            "inst_name": name,
            "cc": cc,
            "method": "unresolved_no_cache",
            "host_openalex_id": None,
            "matched_name": None,
            "ror": None,
        })

    bridge.to_csv(BRIDGE, index=False)
    pd.DataFrame(audit).to_csv(AUDIT, index=False)

    after = pd.read_csv(BRIDGE)
    missing_after = int(after["host_openalex_id"].isna().sum())
    unchanged_non_null = bool(
        (before.loc[~missing_mask, "host_openalex_id"].fillna("") == after.loc[~missing_mask, "host_openalex_id"].fillna("")).all()
    )
    print("missing after:", missing_after)
    print("existing non-null rows unchanged:", unchanged_non_null)
    print("total bridge rows after:", len(after))
    print("audit rows:", len(audit))
    print("unresolved:", [(a["row_id"], a["inst_name"]) for a in audit if a["method"].startswith("unresolved")])


if __name__ == "__main__":
    main()
