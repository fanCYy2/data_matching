import pandas as pd
df = pd.read_csv("matched_authors_final.csv", usecols=["row_id", "Researcher(s)", "authorid"]) 
df.to_csv("output.csv", index=False, encoding="utf-8-sig")