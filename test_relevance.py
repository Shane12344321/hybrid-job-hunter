"""Offline fixtures for deterministic role-relevance triage."""
import unittest

import hybrid_hunter as hh


CONFIG = {"role_relevance": {"mode": "shadow"}}


# Keep the fixture labels stable: they make disagreements in a future policy
# change easy to identify without relying on test ordering.
FIXTURES = [
    ("accept-software", "Software Engineer Intern", {}, "accepted"),
    ("accept-developer", "Developer Intern", {}, "accepted"),
    ("accept-backend", "Backend Engineering Intern", {}, "accepted"),
    ("accept-frontend", "Frontend Developer Intern", {}, "accepted"),
    ("accept-full-stack", "Full-Stack Intern", {}, "accepted"),
    ("accept-devops", "DevOps Intern", {}, "accepted"),
    ("accept-sre", "Site Reliability Intern", {}, "accepted"),
    ("accept-ml", "Machine Learning Intern", {}, "accepted"),
    ("accept-deep-learning", "Deep Learning Intern", {}, "accepted"),
    ("accept-ai", "Artificial Intelligence Intern", {}, "accepted"),
    ("accept-data-science", "Data Science Intern", {}, "accepted"),
    ("accept-data-scientist", "Data Scientist Intern", {}, "accepted"),
    ("accept-data-engineer", "Data Engineer Intern", {}, "accepted"),
    ("accept-nlp", "NLP Research Intern", {}, "accepted"),
    ("accept-computer-vision", "Computer Vision Intern", {}, "accepted"),
    ("accept-quant", "Quantitative Research Intern", {}, "accepted"),
    ("accept-algo-trading", "Algorithmic Trading Intern", {}, "accepted"),
    ("accept-cyber", "Cybersecurity Intern", {}, "accepted"),
    ("accept-appsec", "Application Security Intern", {}, "accepted"),
    ("accept-security-engineer", "Security Engineer Intern", {}, "accepted"),
    ("accept-product-manager", "Product Manager Intern", {}, "accepted"),
    ("accept-product-management", "Product Management Intern", {}, "accepted"),
    ("accept-tech-consulting", "Technology Consulting Intern", {}, "accepted"),
    ("accept-technical-consultant", "Technical Consultant Intern", {}, "accepted"),
    ("accept-business-analyst", "Business Analyst Intern", {}, "accepted"),
    ("accept-data-analyst", "Data Analyst Intern", {}, "accepted"),
    ("accept-bi-analyst", "Business Intelligence Analyst Intern", {}, "accepted"),
    ("accept-context-duty", "Research Intern", {"description": "Conduct machine learning experiments and analyze model results."}, "accepted"),
    ("accept-context-department", "Analyst Intern", {"department": "Data Science"}, "accepted"),
    ("accept-context-category", "Research Intern", {"category": "Software Development"}, "accepted"),
    ("review-analyst", "Analyst Intern", {}, "review"),
    ("review-research", "Research Intern", {}, "review"),
    ("review-researcher", "Researcher Intern", {}, "review"),
    ("review-security", "Security Intern", {}, "review"),
    ("review-engineer", "Engineer Intern", {}, "review"),
    ("review-engineering", "Engineering Intern", {}, "review"),
    ("review-no-signal", "Intern", {}, "review"),
    ("review-sales-software-title-conflict", "Software Sales Intern", {}, "review"),
    ("review-software-mechanical-conflict", "Software and Mechanical Engineering Intern", {}, "review"),
    ("review-software-hr-context", "Software Engineer Intern", {"department": "Human Resources"}, "review"),
    ("review-boilerplate-ai", "Research Intern", {"description": "We build machine learning software for customers."}, "review"),
    ("reject-hr", "HR Intern", {}, "rejected"),
    ("reject-human-resources", "Human Resources Intern", {}, "rejected"),
    ("reject-recruiting", "Recruiting Intern", {}, "rejected"),
    ("reject-talent", "Talent Acquisition Intern", {}, "rejected"),
    ("reject-sales", "Sales Intern", {}, "rejected"),
    ("reject-sales-software-metadata", "Sales Intern", {"department": "Software"}, "rejected"),
    ("reject-marketing", "Marketing Intern", {}, "rejected"),
    ("reject-growth-marketing", "Growth Marketing Intern", {}, "rejected"),
    ("reject-accounting", "Accounting Intern", {}, "rejected"),
    ("reject-accountant", "Accountant Intern", {}, "rejected"),
    ("reject-clinical", "Clinical Research Intern", {}, "rejected"),
    ("reject-nurse", "Nursing Intern", {}, "rejected"),
    ("reject-mechanical", "Mechanical Engineering Intern", {}, "rejected"),
    ("reject-electrical", "Electrical Engineering Intern", {}, "rejected"),
    ("reject-civil", "Civil Engineering Intern", {}, "rejected"),
    ("reject-hardware", "Hardware Engineering Intern", {}, "rejected"),
    ("reject-context-accounting", "Analyst Intern", {"department": "Accounting"}, "rejected"),
    ("reject-context-clinical", "Research Intern", {"category": "Clinical Research"}, "rejected"),
]


class TestRoleRelevance(unittest.TestCase):
    def test_labeled_fixture_matrix(self):
        self.assertGreaterEqual(len(FIXTURES), 50)
        for label, title, context, expected in FIXTURES:
            with self.subTest(label=label):
                result = hh.classify_role(
                    {"title": title, "role_context": context}, CONFIG)
                self.assertEqual(result["verdict"], expected)
                self.assertIsInstance(result["reason"], str)
                self.assertIsInstance(result["evidence"], list)

    def test_modes_annotate_without_filtering(self):
        jobs = [{"id": "a", "title": "Sales Intern"},
                {"id": "b", "title": "Software Intern"}]
        for mode in ("off", "shadow", "enforce"):
            result = hh.annotate_role_relevance(
                jobs, {"role_relevance": {"mode": mode}})
            self.assertEqual([job["id"] for job in result], ["a", "b"])
            if mode == "off":
                self.assertNotIn("relevance", result[0])
            else:
                self.assertIn("relevance", result[0])

    def test_custom_phrase_lists_are_supported(self):
        config = {"role_relevance": {
            "mode": "shadow", "positive_phrases": ["robotics"],
            "negative_phrases": ["legal"],
        }}
        self.assertEqual(hh.classify_role({"title": "Robotics Intern"}, config)["verdict"], "accepted")
        self.assertEqual(hh.classify_role({"title": "Legal Intern"}, config)["verdict"], "rejected")


if __name__ == "__main__":
    unittest.main()
