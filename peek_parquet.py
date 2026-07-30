import sys
import glob
import pandas as pd
import pyarrow.parquet as pq

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)

files = sys.argv[1:] or glob.glob("**/*.parquet", recursive=True)

for path in files:
    print("=" * 80)
    print(path)
    try:
        meta = pq.ParquetFile(path).metadata
        print(f"rows: {meta.num_rows}  cols: {meta.num_columns}")
        # 只读前几行，避免加载整个大文件
        df = next(pq.ParquetFile(path).iter_batches(batch_size=3)).to_pandas()
        print("-" * 40)
        print(df.dtypes)
        print("-" * 40)
        print(df.head(3))
    except Exception as e:
        print("ERROR:", e)
    print()

# python -c "import pandas as pd; df=pd.read_parquet('sciscinet_authors.parquet'); print(df.head()); print(df.shape); print(df.dtypes)"
