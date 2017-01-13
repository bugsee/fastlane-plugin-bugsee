describe Fastlane::Actions::BugseeAction do
  describe '#run' do
    it 'prints a message' do
      expect(Fastlane::UI).to receive(:message).with("The bugsee plugin is working!")

      Fastlane::Actions::BugseeAction.run(nil)
    end
  end
end
