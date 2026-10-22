from fastapi import FastAPI
from pydantic import BaseModel
from bigtrooper_qr import QRModelOptions,QRType, generate_qr_model


app = FastAPI()


@app.get("/up")
def read_root():
    return {"status": "ok"}


# options = QRModelOptions(data="hello", base_width=30, base_height=30)
# result = generate_qr_model(options)

class Tag(BaseModel):
    pass

@app.post("/tag/")
def create_tag(tag: Tag):
    return {"name": tag.name, "description": tag.description, "price": tag.price, "tax": tag.tax}