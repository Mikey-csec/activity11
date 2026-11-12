import os

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load MONGODB_URI from a local .env file (locally) or from the environment (Render).
load_dotenv()

client = MongoClient(os.environ["MONGODB_URI"])
db = client["cs3720db"]
collection = db["sculptures"]

# Create the FastAPI application and allow browser-based clients such as Expo to call the API.
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Shared validation rules for sculpture creation and updates.
class SculptureValidated(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )
    name: str = Field(min_length=1)
    artist: str = Field(min_length=1)
    description: str = Field(min_length=1)
    imageUrl: HttpUrl
    alt: str = Field(min_length=1)


# Return a consistent validation error payload so clients receive readable API feedback.
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Malformed, missing, or invalid input should return a 400 with a readable detail string."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "path"))
        msg = err.get("msg", "Invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "; ".join(parts) or "Invalid request"},
    )


# Handle duplicate-name conflicts from MongoDB constraints in a client-friendly way.
@app.exception_handler(DuplicateKeyError)
async def duplicate_handler(request: Request, exc: DuplicateKeyError):
    return JSONResponse(status_code=409, content={"detail": "Sculpture already exists"})


# Surface database connectivity failures without exposing internal MongoDB details.
@app.exception_handler(PyMongoError)
async def database_error_handler(request: Request, exc: PyMongoError):
    print("Database error:", repr(exc))
    return JSONResponse(status_code=503, content={"detail": "Database not reachable"})


# Catch unexpected server issues and return a generic 500 response.
@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    print("Unexpected error:", repr(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def serialize(doc: dict) -> dict:
    # MongoDB ObjectId values are not JSON serializable by default, so convert them before returning results.
    doc["_id"] = str(doc["_id"])
    return doc


def require_name(name: str) -> str:
    # Reject blank names before querying the database so empty values are never accepted.
    if not name.strip():
        raise HTTPException(status_code=400, detail="Sculpture name cannot be blank")
    return name


def parse_object_id(sculpture_id: str) -> ObjectId:
    # Reject malformed ids with a 400 instead of letting bson raise an unexpected error.
    try:
        return ObjectId(sculpture_id.strip())
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid sculpture id")


# Lightweight health-check endpoint used to verify the service is running.
@app.get("/hello")
def read_root():
    return {"message": "Hello from FastAPI + MongoDB Atlas backend!"}


# Return every sculpture document currently stored in MongoDB.
@app.get("/sculptures")
def get_sculptures():
    return [serialize(s) for s in collection.find()]


# Create a new sculpture record after verifying the name is not already in use.
@app.post("/sculptures", status_code=status.HTTP_201_CREATED)
def add_sculpture(sculpture: SculptureValidated):
    if collection.find_one({"name": sculpture.name}):
        raise HTTPException(status_code=409, detail="Sculpture already exists")
    # Convert HttpUrl to a JSON-safe string before writing to MongoDB.
    result = collection.insert_one(sculpture.model_dump(mode="json"))
    return {"message": "Sculpture added successfully", "id": str(result.inserted_id)}


# Update an existing sculpture (found by its current name) while preventing duplicate names.
@app.put("/sculptures/{name}")
def update_sculpture(sculpture: SculptureValidated, name: str = Path(min_length=1)):
    require_name(name)
    existing = collection.find_one({"name": name})
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Sculpture '{name}' not found")

    # Guard against renaming a sculpture to a name already used by another record.
    duplicate = collection.find_one(
        {"name": sculpture.name, "_id": {"$ne": existing["_id"]}}
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409, detail="Another sculpture already has that name"
        )

    result = collection.update_one(
        {"_id": existing["_id"]}, {"$set": sculpture.model_dump(mode="json")}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Sculpture no longer exists")
    if result.modified_count == 0:
        return {
            "message": "No changes needed; sculpture already has these values",
            "id": str(existing["_id"]),
        }
    return {"message": "Sculpture updated successfully", "id": str(existing["_id"])}


# Delete a sculpture by its MongoDB _id.
@app.delete("/sculptures/{sculpture_id}")
def delete_sculpture(sculpture_id: str = Path(min_length=1)):
    object_id = parse_object_id(sculpture_id)
    existing = collection.find_one({"_id": object_id})
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Sculpture not found. It may have already been deleted",
        )

    result = collection.delete_one({"_id": object_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Sculpture no longer exists")
    return {
        "message": f"Sculpture '{existing['name']}' deleted successfully",
        "id": sculpture_id,
    }