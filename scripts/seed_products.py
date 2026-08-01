"""
Seeds a realistic starting catalog: DSA/MAANG interview prep, math for
ML, data analyst/data science tracks, and the full applied-AI ladder
(deep learning, NLP, CV, RL, LLMs end-to-end, agentic AI, RAG,
fine-tuning), plus a product-building course — matching the catalog
scope this platform is actually meant to teach.

Idempotent by slug: re-running this after a course already exists
skips it rather than creating a duplicate, so it's safe to run again
after adding new entries to CATALOG below.

Usage:
    python -m scripts.seed_products

Ratings below are static seed values standing in for "this course has
an established track record" — see the note in app/db/models/product.py
on why the admin CRUD form does NOT let anyone set this directly.
"""

from decimal import Decimal

from app.db.session import SessionLocal
from app.products import service

CATALOG = [
    dict(
        title="Data Structures & Algorithms for MAANG Interviews",
        category="Interview Prep", subcategory="DSA", level="intermediate",
        price=Decimal("4999"), duration_minutes=2400, rating=4.8,
        instructor="Priya Raman",
        tags=["dsa", "interview", "coding", "arrays", "trees", "graphs", "dynamic-programming"],
        description=(
            "A structured, interview-first pass through data structures and algorithms — "
            "arrays, strings, trees, graphs, heaps, and dynamic programming — with the "
            "pattern-recognition approach top tech companies actually test for, not just "
            "problem-by-problem memorization."
        ),
    ),
    dict(
        title="System Design for Senior Engineering Interviews",
        category="Interview Prep", subcategory="System Design", level="advanced",
        price=Decimal("5999"), duration_minutes=1800, rating=4.7,
        instructor="Arjun Mehta",
        tags=["system-design", "scalability", "interview", "distributed-systems"],
        description=(
            "Design large-scale systems the way senior/staff interview loops actually probe "
            "them: load balancing, caching, sharding, consistency tradeoffs, and how to "
            "narrate a design under interview time pressure."
        ),
    ),
    dict(
        title="MAANG Behavioral Interview Mastery",
        category="Interview Prep", subcategory="Behavioral", level="beginner",
        price=Decimal("1999"), duration_minutes=480, rating=4.5,
        instructor="Divya Kapoor",
        tags=["behavioral", "interview", "communication"],
        description=(
            "Structure compelling answers to behavioral and leadership-principle interviews "
            "using a repeatable framework, with real examples calibrated to what senior "
            "interviewers are actually listening for."
        ),
    ),
    dict(
        title="Mathematics for Machine Learning",
        category="Foundations", subcategory="Math for ML", level="beginner",
        price=Decimal("2999"), duration_minutes=1500, rating=4.6,
        instructor="Rohan Iyer",
        tags=["linear-algebra", "calculus", "probability", "statistics", "math"],
        description=(
            "The linear algebra, calculus, probability, and statistics that actually show up "
            "in ML papers and model internals — built up from first principles, in the order "
            "you'll need it for the rest of this catalog."
        ),
    ),
    dict(
        title="Python for Machine Learning",
        category="Foundations", subcategory="Python for ML", level="beginner",
        price=Decimal("1999"), duration_minutes=900, rating=4.5,
        instructor="Sneha Nair",
        tags=["python", "numpy", "pandas", "programming"],
        description=(
            "Practical Python for ML work: NumPy, pandas, vectorized thinking, and the coding "
            "habits that separate 'runs once in a notebook' from 'ships in a pipeline.'"
        ),
    ),
    dict(
        title="Data Analyst Career Track",
        category="Data", subcategory="Data Analytics", level="beginner",
        price=Decimal("3999"), duration_minutes=2100, rating=4.6,
        instructor="Karthik Subramanian",
        tags=["sql", "excel", "tableau", "data-analysis", "dashboards"],
        description=(
            "SQL, spreadsheet modeling, and dashboarding (Tableau) for a data analyst role — "
            "ending with a portfolio of real dashboards built on public datasets."
        ),
    ),
    dict(
        title="Data Scientist / ML Engineer Track",
        category="Data", subcategory="Data Science", level="intermediate",
        price=Decimal("6999"), duration_minutes=3000, rating=4.7,
        instructor="Ananya Krishnan",
        tags=["machine-learning", "mle", "feature-engineering", "mlops"],
        description=(
            "From feature engineering through model evaluation to shipping a model behind an "
            "API — the full data scientist / ML engineer loop, not just the modeling notebook."
        ),
    ),
    dict(
        title="Deep Learning End-to-End",
        category="Applied AI", subcategory="Deep Learning", level="intermediate",
        price=Decimal("5499"), duration_minutes=2400, rating=4.7,
        instructor="Vikram Rao",
        tags=["deep-learning", "neural-networks", "pytorch", "cnn", "backpropagation"],
        description=(
            "Neural networks from backpropagation up through modern architectures in PyTorch — "
            "training, debugging, and the intuition for why a network isn't learning."
        ),
    ),
    dict(
        title="NLP End-to-End",
        category="Applied AI", subcategory="NLP", level="intermediate",
        price=Decimal("5499"), duration_minutes=2100, rating=4.6,
        instructor="Meera Pillai",
        tags=["nlp", "transformers", "text-classification", "embeddings"],
        description=(
            "Text classification, embeddings, and the transformer architecture that underlies "
            "modern NLP — building up to the same building blocks LLMs are made of."
        ),
    ),
    dict(
        title="Computer Vision End-to-End",
        category="Applied AI", subcategory="Computer Vision", level="intermediate",
        price=Decimal("5499"), duration_minutes=2100, rating=4.5,
        instructor="Aditya Menon",
        tags=["computer-vision", "cnn", "object-detection", "opencv"],
        description=(
            "CNNs, object detection, and practical OpenCV pipelines — from a first classifier "
            "to a working detection pipeline on real images."
        ),
    ),
    dict(
        title="Reinforcement Learning: Foundations to Advanced",
        category="Applied AI", subcategory="Reinforcement Learning", level="advanced",
        price=Decimal("5999"), duration_minutes=1800, rating=4.4,
        instructor="Nikhil Bhatt",
        tags=["reinforcement-learning", "q-learning", "policy-gradient", "rl"],
        description=(
            "Q-learning through policy gradients, with the math made concrete via "
            "implementations you run yourself rather than take on faith."
        ),
    ),
    dict(
        title="Large Language Models End-to-End",
        category="Generative AI", subcategory="LLMs", level="advanced",
        price=Decimal("7999"), duration_minutes=2400, rating=4.8,
        instructor="Ishaan Verma",
        tags=["llm", "transformers", "gpt", "pretraining"],
        description=(
            "How LLMs are actually built: tokenization, pretraining objectives, scaling laws, "
            "and inference — the foundation the rest of the Generative AI track builds on."
        ),
    ),
    dict(
        title="Agentic AI Systems",
        category="Generative AI", subcategory="Agentic AI", level="advanced",
        price=Decimal("7999"), duration_minutes=2100, rating=4.7,
        instructor="Kavya Desai",
        tags=["agentic-ai", "agents", "langgraph", "tool-use", "planning"],
        description=(
            "Design LLM agents with real tool use, planning, and controlled execution — the "
            "same deterministic-vs-probabilistic discipline this platform's own recommendation "
            "agent is built on."
        ),
    ),
    dict(
        title="Retrieval-Augmented Generation (RAG) in Production",
        category="Generative AI", subcategory="RAG", level="advanced",
        price=Decimal("6999"), duration_minutes=1800, rating=4.7,
        instructor="Rahul Chatterjee",
        tags=["rag", "vector-search", "embeddings", "retrieval"],
        description=(
            "Chunking, embeddings, vector search, and grounding — build a RAG pipeline that "
            "answers from real documents instead of hallucinating from parametric memory."
        ),
    ),
    dict(
        title="LLM Fine-Tuning & Adaptation",
        category="Generative AI", subcategory="Fine-tuning", level="advanced",
        price=Decimal("7499"), duration_minutes=1800, rating=4.6,
        instructor="Tanya Joseph",
        tags=["fine-tuning", "lora", "peft", "llm"],
        description=(
            "LoRA, PEFT, and full fine-tuning tradeoffs for adapting an LLM to a specific "
            "domain or task — including when fine-tuning is the wrong tool and RAG isn't."
        ),
    ),
    dict(
        title="Building AI Products That Ship",
        category="Product", subcategory="AI Product Building", level="intermediate",
        price=Decimal("4999"), duration_minutes=1500, rating=4.5,
        instructor="Sanjay Kulkarni",
        tags=["product", "shipping", "ai-products", "ux"],
        description=(
            "Turn an AI capability into a product a user actually trusts and keeps using — "
            "framing, UX around uncertainty, and the deterministic-vs-AI boundary in your own "
            "architecture."
        ),
    ),
]


def main() -> None:
    db = SessionLocal()
    created, skipped = 0, 0
    try:
        for entry in CATALOG:
            slug = service.slugify(entry["title"])
            try:
                service.get_product_by_slug(db, slug, include_archived=True)
                skipped += 1
                continue
            except service.ProductNotFound:
                pass

            service.create_product(
                db,
                title=entry["title"],
                description=entry["description"],
                category=entry["category"],
                subcategory=entry["subcategory"],
                price=entry["price"],
                level=entry["level"],
                tags=entry["tags"],
                instructor=entry["instructor"],
                duration_minutes=entry["duration_minutes"],
                image_url=None,
                rating=entry["rating"],
            )
            created += 1
        print(f"Seeded {created} course(s), skipped {skipped} already present.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
