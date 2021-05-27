from setuptools import setup

setup(
    name="cgtop_mon",
    version="1.0",
    author="Maik Schmidt",
    python_requires=">=3.5",
    packages=["cgtop_mon"],
    scripts=[],
    entry_points="""
      [console_scripts]
      cgtop_mon=cgtop_mon
      """,
    install_requires=[
        "influxdb",
    ]
)
