use clap::Parser;

#[derive(Parser)]
#[command(about = "Print a greeting")]
struct Args {
    #[arg(long)]
    name: String,
}

fn main() {
    let args = Args::parse();
    println!("Hello, {}!", args.name);
}
