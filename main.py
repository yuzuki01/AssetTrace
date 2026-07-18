from sina.index import Index

idx = Index("laodeng.json")
idx.backfill("2020-01-01")
idx.plot("sh000001", "sh000680", use_old=True)
idx.info(use_old=True)
